// Runtime interaction tests for config_ui.html without a browser dependency.
// The shipped script is evaluated with a deliberately small DOM facade so the
// dirty-state and rendering contracts stay testable with plain Node.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import { scriptSource } from "./extract.mjs";

const ROOT = path.dirname(path.dirname(path.dirname(fileURLToPath(import.meta.url))));
const HTML = readFileSync(path.join(ROOT, "shellui", "config_ui.html"), "utf8");
// 脚本提取走 extract.mjs 单一归宿（HTML 仍本地读取——CSS 断言用）
const SCRIPT = scriptSource().replace(/\nload\(\);\s*$/, "\n");

test("workbench regions stay pinned when the pending bar is hidden", () => {
  assert.match(
    HTML,
    /\.workbench\{[^}]*grid-template-areas:"header" "pending" "content" "status"/,
  );
  assert.match(HTML, /\.app-header\{grid-area:header;/);
  assert.match(HTML, /\.pending-bar\{grid-area:pending;/);
  assert.match(HTML, /\.content-viewport\{grid-area:content;/);
  assert.match(HTML, /\.statusbar\{grid-area:status;/);
});

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(name) { this.values.add(name); }
  remove(name) { this.values.delete(name); }
  toggle(name, force) {
    const on = force === undefined ? !this.values.has(name) : Boolean(force);
    if (on) this.values.add(name); else this.values.delete(name);
    return on;
  }
  contains(name) { return this.values.has(name); }
}

function fakeElement(extra = {}) {
  const attrs = new Map();
  return {
    textContent: "",
    innerHTML: "",
    value: "",
    checked: false,
    disabled: false,
    hidden: false,
    title: "",
    classList: new FakeClassList(),
    style: {},
    dataset: {},
    addEventListener() {},
    setAttribute(name, value) { attrs.set(name, String(value)); },
    getAttribute(name) { return attrs.has(name) ? attrs.get(name) : null; },
    querySelector() { return null; },
    ...extra,
  };
}

function makeRuntime(fetchImpl) {
  const elements = new Map();
  for (const id of [
    "save-btn", "status-left", "shortcut-hint", "pending-bar",
    "pending-title", "pending-items", "pending-save-btn", "viewport",
    "nav-container", "page-title", "page-subtitle", "toast", "toast-msg",
    "probe-ssh", "probe-vpn",
  ]) elements.set(id, fakeElement());

  for (const id of ["cfg-sysproxy", "cfg-sleep", "cfg-login"]) {
    const label = fakeElement();
    const el = fakeElement({
      parentElement: { querySelector: () => label },
    });
    el.setAttribute("aria-checked", "false");
    elements.set(id, el);
  }

  const document = {
    getElementById(id) { return elements.get(id) || null; },
    querySelector() { return null; },
    addEventListener() {},
  };
  // servers 视图的 enter 会启动 nfsPollLoop（自续 5s 定时器）——测试进程
  // 不得被它拖住：unref 后句柄不阻止进程退出。
  const unrefSetTimeout = (fn, ms, ...args) => {
    const t = setTimeout(fn, ms, ...args);
    if (t && typeof t.unref === "function") t.unref();
    return t;
  };
  const context = vm.createContext({
    console,
    URL,
    URLSearchParams,
    setTimeout: unrefSetTimeout,
    clearTimeout,
    structuredClone,
    location: { search: "", port: "9528" },
    navigator: { clipboard: { writeText: async () => {} } },
    window: { __I18N__: {
      lang: "zh-CN",
      messages: {
        "zh-CN": JSON.parse(readFileSync(path.join(ROOT, "shared", "locales", "zh-CN.json"), "utf8")),
        en: JSON.parse(readFileSync(path.join(ROOT, "shared", "locales", "en.json"), "utf8")),
      },
    } },
    document,
    fetch: fetchImpl || (async () => { throw new Error("unexpected fetch"); }),
  });
  vm.runInContext(SCRIPT, context);
  return {
    elements,
    run(source) { return vm.runInContext(source, context); },
  };
}

test("saveAll keeps edits made while the PUT is pending dirty", async () => {
  let release, submitted;
  const response = new Promise(resolve => { release = resolve; });
  const rt = makeRuntime(async (_url, opts) => {
    submitted = JSON.parse(opts.body);
    await response;
    return { json: async () => ({ ok: true }) };
  });
  rt.run(`
    S=normalizeState({mp:{servers:[],prevent_sleep:false}});
    baselineState=cloneData(S);baselineRoles={};ccRoles={};activeView='system';
    toggleSwitch(document.getElementById('cfg-sleep'));
  `);
  const pending = rt.run("saveAll(false)");
  assert.equal(submitted.mp.prevent_sleep, true);
  rt.run("toggleSwitch(document.getElementById('cfg-sleep'))");
  release();
  await pending;
  assert.equal(rt.run("baselineState.mp.prevent_sleep"), true);
  assert.equal(rt.run("S.mp.prevent_sleep"), false);
  assert.equal(rt.run("dirty"), true);
});

test("a system switch reverted to its baseline clears dirty state", () => {
  const rt = makeRuntime();
  rt.run(`
    window.bridgeMessages=[];
    window.webkit={messageHandlers:{bridge:{postMessage(message){window.bridgeMessages.push(message);}}}};
    S=normalizeState({mp:{system_proxy_default:false,prevent_sleep:false,launch_at_login:false}});
    baselineState=cloneData(S);baselineRoles={};ccRoles={};activeView='system';
    recomputeDirty();
  `);

  rt.run("toggleSwitch(document.getElementById('cfg-sysproxy'))");
  assert.equal(rt.run("dirty"), true);
  assert.equal(rt.run("totalDirtyCount()"), 1);
  assert.equal(rt.elements.get("pending-bar").hidden, false);

  rt.run("toggleSwitch(document.getElementById('cfg-sysproxy'))");
  assert.equal(rt.run("dirty"), false);
  assert.equal(rt.run("totalDirtyCount()"), 0);
  assert.equal(rt.elements.get("pending-bar").hidden, true);
  assert.equal(rt.elements.get("save-btn").disabled, true);
  assert.deepEqual(
    structuredClone(rt.run("window.bridgeMessages.at(-1)")),
    { type: "dirtyState", payload: { dirty: false } },
  );
});

test("read-only pages expose refresh while preserving visible cross-page changes", () => {
  const rt = makeRuntime();
  rt.run(`
    S=normalizeState({mp:{system_proxy_default:false,prevent_sleep:false,launch_at_login:false}});
    baselineState=cloneData(S);baselineRoles={};ccRoles={};activeView='system';
    recomputeDirty();toggleSwitch(document.getElementById('cfg-sysproxy'));
    activeView='usage';updateStatus();
  `);

  assert.equal(rt.elements.get("save-btn").textContent, "刷新");
  assert.equal(rt.elements.get("save-btn").disabled, false);
  assert.equal(rt.elements.get("pending-bar").hidden, false);
  assert.match(rt.elements.get("pending-items").innerHTML, /系统选项 · 1 项/);
  assert.equal(rt.elements.get("shortcut-hint").textContent, "⌘S 保存全部");
  assert.doesNotMatch(rt.elements.get("save-btn").textContent, /保存/);
});

test("a clean read-only page advertises refresh, not save", () => {
  const rt = makeRuntime();
  rt.run(`
    S=normalizeState({});baselineState=cloneData(S);baselineRoles={};ccRoles={};
    activeView='status';recomputeDirty();
  `);
  assert.equal(rt.elements.get("save-btn").textContent, "刷新");
  assert.equal(rt.elements.get("shortcut-hint").textContent, "⌘R 刷新");
});

test("zero servers render a real empty state without a fake Server 1 editor", () => {
  const rt = makeRuntime();
  const html = rt.run("S=normalizeState({mp:{servers:[]}});activeTunnel=0;serversHTML()");
  assert.match(html, /0 台服务器/);
  assert.match(html, /添加第一台服务器/);
  assert.doesNotMatch(html, /Server 1/);
  assert.doesNotMatch(html, /data-tf=/);
});

test("typing then clearing a provider API key restores the masked baseline", () => {
  const rt = makeRuntime();
  rt.run(`
    S=normalizeState({sp:{providers:{p1:{base_url:'',api_key:null,api_key_set:true,
      api_key_env:null,auth_header:null,models:[],anthropic_native:false,enabled:true}}}});
    baselineState=cloneData(S);baselineRoles={};ccRoles={};
    activeProvider='p1';activeView='providers';recomputeDirty();
    const fields={name:{value:'p1'},base_url:{value:''},api_key_env:{value:''},auth_header:{value:''}};
    window.__key={value:''};
    window.__detail={querySelector:function(sel){
      const m=sel.match(/data-pf="(\\w+)"/);
      if(m&&m[1]==='api_key')return window.__key;
      if(m&&fields[m[1]])return fields[m[1]];
      return null;
    }};
    document.querySelector=function(sel){
      return sel.includes('detail-body')?window.__detail:null;
    };
  `);

  rt.run("window.__key.value='sk-typed';collectAndRecompute()");
  assert.equal(rt.run("dirty"), true);
  assert.equal(rt.run("S.sp.providers.p1.api_key"), "sk-typed");

  rt.run("window.__key.value='';collectAndRecompute()");
  assert.equal(rt.run("dirty"), false, "cleared key must clear dirty");
  assert.equal(rt.run("S.sp.providers.p1.api_key"), null,
    "cleared key must not leave a phantom value that would be silently saved");
  assert.equal(rt.run("S.sp.providers.p1.api_key_set"), true,
    "api_key_set is the server's masked truth and is never mutated by typing");
});

test("typing then clearing an SSH password restores the masked baseline", () => {
  const rt = makeRuntime();
  rt.run(`
    S=normalizeState({mp:{servers:[{name:'t1',has_password:true,
      ssh:{user:'',host:'h',port:22,auth_type:'password',ssh_key:'',compression:true},
      services:{ssh:{forwards:[],autostart:false}}}]}});
    baselineState=cloneData(S);baselineRoles={};ccRoles={};
    activeView='servers';activeTunnel=0;recomputeDirty();
    const fields={name:{value:'t1'},addr:{value:'h'},ssh_port:{value:'22'},
      auth:{value:'password'},key:{value:''}};
    window.__pw={value:''};
    window.__detail={querySelector:function(sel){
      const m=sel.match(/data-tf="(\\w+)"/);
      if(m&&m[1]==='pw')return window.__pw;
      if(m&&fields[m[1]])return fields[m[1]];
      if(m&&m[1]==='compress')return{getAttribute:()=>'true'};
      return null;
    },querySelectorAll:function(){return[];}};
    document.querySelector=function(sel){
      return sel.includes('.md-detail')?window.__detail:null;
    };
  `);

  rt.run("window.__pw.value='secret';collectAndRecompute()");
  assert.equal(rt.run("dirty"), true);

  rt.run("window.__pw.value='';collectAndRecompute()");
  assert.equal(rt.run("dirty"), false, "cleared password must clear dirty");
  assert.equal(rt.run("S.mp.servers[0].password"), null,
    "cleared password must not leave a phantom value for the keychain write");
});

test("collectServers reads forward rows into the active server", () => {
  const rt = makeRuntime();
  rt.run(`
    S=normalizeState({mp:{servers:[{name:'t1',
      ssh:{user:'u',host:'h',port:22,auth_type:'key',ssh_key:'',compression:true},
      services:{ssh:{forwards:[],autostart:false}}}]}});
    baselineState=cloneData(S);baselineRoles={};ccRoles={};
    activeView='servers';activeTunnel=0;recomputeDirty();
    const fields={name:{value:'t1'},addr:{value:'u@h'},ssh_port:{value:'22'},
      auth:{value:'key'},key:{value:''}};
    const row=vals=>({querySelector:function(sel){
      const m=sel.match(/data-fwf="(\\w+)"/);
      return m?vals[m[1]]:null;}});
    const rows=[row({local_port:{value:'9000'},remote_host:{value:' 10.0.0.5 '},
      remote_port:{value:'8000'}}),
      row({local_port:{value:''},remote_host:{value:''},remote_port:{value:''}})];
    window.__detail={querySelector:function(sel){
      const m=sel.match(/data-tf="(\\w+)"/);
      if(m&&fields[m[1]])return fields[m[1]];
      if(m&&m[1]==='compress')return{getAttribute:()=>'true'};
      return null;
    },querySelectorAll:function(sel){
      return sel.includes('data-fwr')?rows:[];
    }};
    document.querySelector=function(sel){
      return sel.includes('.md-detail')?window.__detail:null;
    };
  `);
  rt.run("collectServers();recomputeDirty()");
  // vm 跨 realm 对象不走 deepEqual（原型不同）——JSON 字符串钉形状
  assert.equal(rt.run("JSON.stringify(S.mp.servers[0].services.ssh.forwards)"),
    JSON.stringify([
      { local_port: 9000, remote_host: "10.0.0.5", remote_port: 8000,
        enabled: true },
      { local_port: 0, remote_host: "127.0.0.1", remote_port: 0,
        enabled: true },
    ]), "行序即数组序；空白地址 trim 后缺省 127.0.0.1，空端口为 0，"
      + "无开关（缺省）行为启用");
  assert.equal(rt.run("dirty"), true, "新增转发行必须点亮保存按钮");
});

test("server reconnect keeps stable ids through role save and runtime polling", async () => {
  let submitted;
  const rt = makeRuntime(async (url, opts) => {
    if (opts?.method === "PUT") {
      submitted = JSON.parse(opts.body);
      return { ok: true, json: async () => ({ ok: true }) };
    }
    assert.equal(url, "/api/state");
    return { ok: true, json: async () => ({ mp: { servers: [
      { id: "t-proxy", is_proxy: false },
      { id: "t-other", is_proxy: true },
    ] } }) };
  });
  rt.run(`
    S=normalizeState({mp:{proxy_server_id:'t-proxy',servers:[
      {id:'t-proxy',is_proxy:true,ssh:{host:'h'}},
      {id:'t-other',is_proxy:false,ssh:{host:'h2'}}]}});
    baselineState=cloneData(S);baselineRoles={};ccRoles={};
    activeView='servers';activeTunnel=0;
    window.__sent=[];
    window.webkit={messageHandlers:{bridge:{postMessage:m=>window.__sent.push(m)}}};
    document.querySelectorAll=()=>[];
    document.getElementById('viewport').firstElementChild={classList:{add(){}}};
    renderView();
  `);
  const commandInView = () => rt.elements.get("viewport").innerHTML
    .match(/onclick="(reconnectProxy\(this,'[^']*'\))"/)[1];
  const click = (command, id) => {
    rt.run(command.replace("this", "null"));
    assert.deepEqual(JSON.parse(rt.run("JSON.stringify(window.__sent.at(-1))")),
      { type: "reconnectProxy", payload: { tunnel_id: id } });
  };
  const oldProxyButton = commandInView();
  click(oldProxyButton, "t-proxy");
  rt.run("activeTunnel=1;setProxyServer()");
  const newProxyButton = commandInView();
  click(newProxyButton, "t-other");
  assert.equal(rt.run("baselineState.mp.proxy_server_id"), "t-proxy");

  await rt.run("saveAll(false)");
  assert.equal(submitted.mp.proxy_server_id, "t-other");
  assert.equal(rt.run("baselineState.mp.proxy_server_id"), "t-other");
  click(newProxyButton, "t-other");  // 不等待装饰轮询，后端按已保存角色分派
  await rt.run("nfsRefreshRuntime()");
  assert.equal(rt.run("S.mp.servers[1].is_proxy"), true);
  assert.equal(commandInView(), newProxyButton);
  click(commandInView(), "t-other");
  click(oldProxyButton, "t-proxy"); // 旧页面按钮也不能把转发误送为接入
});

test("collectServers reads the forward_autostart switch", () => {
  const rt = makeRuntime();
  rt.run(`
    S=normalizeState({mp:{servers:[{name:'t1',
      ssh:{user:'',host:'h',port:22,auth_type:'key',ssh_key:'',compression:true},
      services:{ssh:{forwards:[],autostart:false}}}]}});
    baselineState=cloneData(S);baselineRoles={};ccRoles={};
    activeView='servers';activeTunnel=0;recomputeDirty();
    const fields={name:{value:'t1'},addr:{value:'h'},ssh_port:{value:'22'},
      auth:{value:'key'},key:{value:''},
      fw_autostart:{getAttribute:()=> 'true'}};
    window.__detail={querySelector:function(sel){
      const m=sel.match(/data-tf="(\\w+)"/);
      if(m&&m[1]==='compress')return{getAttribute:()=>'true'};
      if(m&&m[1]==='fw_autostart')return fields.fw_autostart;
      if(m&&fields[m[1]])return fields[m[1]];
      return null;
    },querySelectorAll:function(){return[];}};
    document.querySelector=function(sel){
      return sel.includes('.md-detail')?window.__detail:null;
    };
  `);
  rt.run("collectServers();recomputeDirty()");
  assert.equal(rt.run("S.mp.servers[0].services.ssh.autostart"), true,
    "autostart 开关经 collect 读回（services.ssh.autostart）");
  assert.equal(rt.run("dirty"), true, "开关翻转点亮保存按钮");
});

test("an nfs-only edit lands on the single servers page in the pending bar", () => {
  // v0.13.0 服务器单视图：ssh/forwards/nfs 合并进一个 'servers' 投影——
  // NFS 编辑不再出现在独立的「远程挂载」页签
  const rt = makeRuntime();
  rt.run(`
    S=normalizeState({mp:{servers:[{id:'t1',name:'n',
      ssh:{user:'',host:'h',port:22,auth_type:'key',ssh_key:'',compression:true},
      services:{ssh:{forwards:[],autostart:false}}}]}});
    baselineState=cloneData(S);baselineRoles={};ccRoles={};
    activeView='servers';activeTunnel=0;recomputeDirty();
    const fields={name:{value:'n'},addr:{value:'h'},ssh_port:{value:'22'},
      auth:{value:'key'},key:{value:''}};
    const nf={enabled:{getAttribute:()=>'true'},squash:{getAttribute:()=>'false'}};
    window.__detail={querySelector:function(sel){
      const m=sel.match(/data-tf="(\\w+)"/);
      if(m&&m[1]==='compress')return{getAttribute:()=>'true'};
      if(m&&m[1]==='fw_autostart')return{getAttribute:()=>'false'};
      if(m&&fields[m[1]])return fields[m[1]];
      const n=sel.match(/data-nf="(\\w+)"/);
      if(n&&nf[n[1]])return nf[n[1]];
      if(n&&n[1]==='port')return{value:'13000'};
      return null;
    },querySelectorAll:function(){return[];}};
    document.querySelector=function(sel){
      return sel.includes('.md-detail')?window.__detail:null;
    };
  `);
  rt.run("collectAndRecompute()");
  assert.equal(rt.run("dirty"), true);
  assert.equal(rt.run("S.mp.servers[0].services.nfs.enabled"), true,
    "NFS 启用开关经单一 collectServers 读回");
  assert.match(rt.elements.get("pending-items").innerHTML, /服务器 · 2 项/,
    "enabled 翻转 + 端口变更都记在「服务器」一页（nfsProjection 缺省不产生假 dirty）");
});

// ── 代理角色：查看 ≠ 切换——隐式写已删，角色只经 setProxyServer 显式变更 ──
function setupServerForm(rt, { role = "t-a", active = 0 } = {}) {
  const name = active === 0 ? "A" : "B", addr = active === 0 ? "a" : "b";
  rt.run(`
    S=normalizeState({mp:{proxy_server_id:'${role}',servers:[
      {id:'t-a',name:'A',ssh:{user:'',host:'a',port:22,auth_type:'key',ssh_key:'',compression:true},services:{ssh:{forwards:[],autostart:false}}},
      {id:'t-b',name:'B',ssh:{user:'',host:'b',port:22,auth_type:'key',ssh_key:'',compression:true},services:{ssh:{forwards:[],autostart:false}}}]}});
    baselineState=cloneData(S);baselineRoles={};ccRoles={};
    activeView='servers';activeTunnel=${active};recomputeDirty();
    const fields={name:{value:'${name}'},addr:{value:'${addr}'},ssh_port:{value:'22'},
      auth:{value:'key'},key:{value:''}};
    window.__detail={querySelector:function(sel){
      const m=sel.match(/data-tf="(\\w+)"/);
      if(m&&m[1]==='compress')return{getAttribute:()=>'true'};
      if(m&&m[1]==='fw_autostart')return{getAttribute:()=>'false'};
      if(m&&fields[m[1]])return fields[m[1]];
      return null;
    },querySelectorAll:function(){return[];}};
    document.querySelector=function(sel){
      return sel.includes('.md-detail')?window.__detail:null;
    };
    document.querySelectorAll=function(){return[];};
    document.getElementById('viewport').firstElementChild={classList:{add(){}}};
  `);
}

test("viewing another server must not silently switch the proxy role", () => {
  const rt = makeRuntime();
  setupServerForm(rt, { role: "t-a", active: 1 });
  // 保存路径的精确复现：用户停留在服务器页查看 B（activeTunnel=1）时按下保存
  rt.run("collect(true);recomputeDirty()");
  assert.equal(rt.run("S.mp.proxy_server_id"), "t-a",
    "collect 只读表单——正在查看的服务器绝不能被隐式写成代理服务器");
  assert.equal(rt.run("dirty"), false, "单纯查看另一台服务器不得伪造待保存项");
});

test("setProxyServer marks the role switch as one tracked, reversible change", () => {
  const rt = makeRuntime();
  setupServerForm(rt, { role: "t-a", active: 1 });
  assert.match(rt.run("serversHTML()"), /设为代理服务器/,
    "非已保存服务器的详情栏必须暴露显式角色动作");
  rt.run("setProxyServer()");
  assert.equal(rt.run("S.mp.proxy_server_id"), "t-b");
  assert.equal(rt.run("dirty"), true);
  assert.equal(rt.run("totalDirtyCount()"), 1, "只有角色一个叶子计入待保存");
  assert.match(rt.run("serversHTML()"), /fw-badge[^>]*>✓ 当前代理服务器</,
    "当前代理服务器渲染徽标而非按钮");
  rt.run("discardAll()");
  assert.equal(rt.run("S.mp.proxy_server_id"), "t-a", "放弃更改恢复已保存的角色");
});

test("deleting a server keeps the proxy role on the same server", () => {
  const rt = makeRuntime();
  rt.run(`
    S=normalizeState({mp:{proxy_server_id:'t-c',servers:[
      {id:'t-a',name:'A',ssh:{user:'',host:'a',port:22,auth_type:'key',ssh_key:'',compression:true},services:{ssh:{forwards:[],autostart:false}}},
      {id:'t-b',name:'B',ssh:{user:'',host:'b',port:22,auth_type:'key',ssh_key:'',compression:true},services:{ssh:{forwards:[],autostart:false}}},
      {id:'t-c',name:'C',ssh:{user:'',host:'c',port:22,auth_type:'key',ssh_key:'',compression:true},services:{ssh:{forwards:[],autostart:false}}}]}});
    baselineState=cloneData(S);baselineRoles={};ccRoles={};
    activeView='servers';activeTunnel=2;recomputeDirty();
    document.querySelector=function(){return null;};
    document.querySelectorAll=function(){return[];};
    document.getElementById('viewport').firstElementChild={classList:{add(){}}};
  `);
  rt.run("removeServer(0)");
  assert.equal(rt.run("S.mp.proxy_server_id"), "t-c",
    "删掉代理前面的服务器后，角色 id 纹丝不动——不再依赖下标");
  assert.equal(rt.run("proxyIndexOf(S)"), 1, "解析下标指向同一条服务器");
  assert.equal(rt.run("S.mp.servers[proxyIndexOf(S)].id"), "t-c");
  rt.run("removeServer(1)");
  assert.equal(rt.run("S.mp.servers.length"), 1);
  assert.equal(rt.run("S.mp.proxy_server_id"), "",
    "删掉代理自身后清空 id 真相，交由首条兜底回落");
  assert.equal(rt.run("proxyIndexOf(S)"), 0);
});

// ── 服务器单视图（v0.13.0）：master 标签 + 服务卡渲染 ──────────
test("servers view renders master-detail with proxy badge, service tags and mount rows", () => {
  const rt = makeRuntime();
  const html = rt.run(`
    S=normalizeState({mp:{servers:[
      {id:'t-1',name:'srv-a',ssh:{user:'u',host:'a.example',port:22},
       services:{ssh:{forwards:[{local_port:9000,remote_host:'127.0.0.1',remote_port:80,enabled:true},
                                {local_port:9001,remote_host:'127.0.0.1',remote_port:81,enabled:false}]},
                  nfs:{enabled:true,local_port:12049,squash_to_ssh_user:false,
            mounts:[{name:'data',remote_path:'/data',local_dir:'',auto_mount:true}]}},
       nfs_states:{data:'mounted'}},
      {id:'t-2',name:'srv-b',ssh:{user:'u',host:'b.example',port:22},
       services:{nfs:{enabled:false,local_port:12049,squash_to_ssh_user:false,mounts:[]}}},
    ]}});
    activeTunnel=0;serversHTML();
  `);
  // master：服务器列表 + 代理徽标 + 转发 n/m + NFS ×n 标签（图标 chip：12px 图标在文字前）
  assert.match(html, /class="md-master"/);
  assert.match(html, /selectServer\(1\)/);
  assert.match(html, /tag proxy"><svg[\s\S]*?<\/svg>代理</);
  assert.match(html, /转发 1\/2</);
  assert.match(html, /NFS ×1</);
  // detail：连接 pane + 端口映射 pane（转发表 + 探针区）+ NFS pane（挂载行 + 即时操作）
  assert.match(html, /class="md-detail"/);
  assert.match(html, /data-tf="addr"/);
  assert.match(html, /data-svc-pane="fw"/);
  assert.match(html, /id="probe-ssh"/);
  assert.match(html, /data-tf="fw_autostart"/);
  assert.match(html, /data-fwf="local_port"/);
  assert.match(html, /data-nf="enabled"/);
  assert.match(html, /data-nf="port"/);
  assert.match(html, /data-nf="squash"/);
  assert.match(html, /已挂载/);
  assert.match(html, /nfsMountAction\(this,0,'mount'\)/);
  assert.match(html, /nfsCheckRemote\(this\)/);
  assert.match(html, /svcCheck\('ssh',this\)/);
  // OpenVPN 卡（M2 转正）：桥接式文件选择（原生）/动态 input（浏览器）+
  // 凭证/安装/连接表单 + 检测（svcCheck）
  assert.match(html, /OpenVPN 服务/);
  assert.match(html, /vpnPickProfile\(this\)/);
  assert.match(html, /vpnToggle\(this\)/);
  assert.match(html, /svcCheck\('openvpn',this\)/);
  assert.match(html, /id="probe-vpn"/);
});

// ── 服务 tab（v0.13.0 真机验收反馈：服务卡纵向堆叠 → 横向 tab）─────────
test("service tabs render in fixed order with live counts and all four panes", () => {
  const rt = makeRuntime();
  const html = rt.run(`
    S=normalizeState({mp:{servers:[
      {id:'t-1',name:'srv-a',ssh:{user:'u',host:'a.example',port:22},
       services:{ssh:{forwards:[{local_port:9000,remote_host:'127.0.0.1',remote_port:80,enabled:true},
                                {local_port:9001,remote_host:'127.0.0.1',remote_port:81,enabled:false}]},
                  nfs:{enabled:true,local_port:12049,squash_to_ssh_user:false,
            mounts:[{name:'data',remote_path:'/data',local_dir:'',auto_mount:true}]}}}]}});
    activeTunnel=0;activeSvcTab='conn';serversHTML();
  `);
  // tab 条：四枚 tab 固定顺序（连接/端口映射/NFS/OpenVPN）
  assert.match(html, /class="svc-tabs"/);
  assert.deepEqual(
    [...html.matchAll(/data-svc-tab="(\w+)"/g)].map((m) => m[1]),
    ["conn", "fw", "nfs", "vpn"]);
  // tab 标签计数：端口映射 enabled/total、NFS ×挂载数（OpenVPN M2 转正
  // 后无「即将支持」徽标）
  assert.match(html, /端口映射<span class="svc-tab-count">1\/2<\/span>/);
  assert.match(html, /NFS<span class="svc-tab-count">×1<\/span>/);
  assert.doesNotMatch(html, /class="soon"/);
  // 缺省 tab = 连接：conn pane 可见，其余 hidden——但四 pane 全量渲染
  //（hidden pane 里的 data-* / id 是 collectServers 刮全页、NFS 5s 轮询
  // 与探针结果定向更新的前提，绝不能条件性不渲染）
  assert.match(html, /class="svc-tab is-active" data-svc-tab="conn"/);
  assert.match(html, /class="svc-tab " data-svc-tab="fw"/);
  assert.match(html, /data-svc-pane="conn" >/);
  assert.match(html, /data-svc-pane="fw" hidden>[\s\S]*?data-fwf="local_port"/);
  assert.match(html, /data-svc-pane="nfs" hidden>[\s\S]*?data-nf="port"/);
  assert.match(html, /data-svc-pane="vpn" hidden>[\s\S]*?id="probe-vpn"/);
});

// ── 图标体系（ICONS 注册表 + icon() 单一归宿，Lucide 单笔触语言）─────────
test("icon() emits the shared stroke language at the requested ladder size", () => {
  const rt = makeRuntime();
  const html = rt.run("icon('server', 14)");
  assert.match(html, /^<svg viewBox="0 0 24 24" width="14" height="14"/);
  assert.match(html,
    /fill="none" stroke="currentColor" stroke-width="1\.6" stroke-linecap="round" stroke-linejoin="round"/);
  assert.match(html, /aria-hidden="true"/);
  assert.match(html, /<rect x="2" y="2"/, "几何体来自 ICONS 注册表");
  // 未知键名必须渲染空图标而非破碎 path——布点处手滑写错键名不炸整页
  assert.equal(rt.run("icon('nope', 12)").includes("<rect"), false);
});

test("every view nav icon and all four service tab icons resolve in the ICONS map", () => {
  const rt = makeRuntime();
  // 侧边栏 8 视图的 icon 字段全部是 ICONS 键名（VIEWS 不再内联 path）
  assert.equal(
    rt.run("Object.values(VIEWS).filter(v=>!ICONS[v.icon]).map(v=>v.title).join()"),
    "", "VIEWS 引用了 ICONS 里不存在的图标名");
  // 服务 tab 四图标（连接/端口映射/NFS/OpenVPN）真实落进 serversHTML
  rt.run(`
    S=normalizeState({mp:{servers:[
      {id:'t-1',name:'srv-a',ssh:{user:'u',host:'a.example',port:22},
       services:{ssh:{forwards:[],autostart:false}}}]}});
    activeTunnel=0;
  `);
  const html = rt.run("serversHTML()");
  for (const name of ["plug", "arrow-right-left", "hard-drive", "shield"]) {
    const geom = rt.run(`ICONS[${JSON.stringify(name)}]`);
    assert.ok(geom, `ICONS 缺 ${name} 几何`);
    assert.ok(html.includes(geom), `${name} 未渲染进服务 tab`);
  }
});

test("svcTab switches panes by pure DOM toggle — no re-render, no collect, no dirty", () => {
  const rt = makeRuntime();
  rt.run(`
    S=normalizeState({mp:{servers:[
      {id:'t-1',name:'srv-a',ssh:{user:'u',host:'a.example',port:22},
       services:{ssh:{forwards:[],autostart:false}}}]}});
    baselineState=cloneData(S);baselineRoles={};ccRoles={};
    activeView='servers';activeTunnel=0;recomputeDirty();
    // 连接 pane 里的未保存编辑——真实 UI 中切 tab 绝不能冲掉它
    window.__addr={value:'u@edited.example'};
    window.__mkCls=()=>{const s=new Set();return{
      add:n=>s.add(n),remove:n=>s.delete(n),
      toggle:(n,f)=>{const on=f===undefined?!s.has(n):!!f;on?s.add(n):s.delete(n);return on;},
      contains:n=>s.has(n)};};
    const pane=k=>({hidden:k!=='conn',dataset:{svcPane:k}});
    const tab=k=>({dataset:{svcTab:k},classList:window.__mkCls(),
      attrs:new Map(),
      setAttribute(n,v){this.attrs.set(n,String(v));},
      getAttribute(n){return this.attrs.has(n)?this.attrs.get(n):'false';}});
    window.__panes=['conn','fw','nfs','vpn'].map(pane);
    window.__panes[0].hostedInput=window.__addr;
    window.__tabs=['conn','fw','nfs','vpn'].map(tab);
    window.__detail={querySelectorAll:function(sel){
      if(sel.includes('data-svc-pane'))return window.__panes;
      if(sel.includes('data-svc-tab'))return window.__tabs;
      return [];
    }};
    document.querySelector=function(sel){
      return sel.includes('.md-detail')?window.__detail:null;
    };
    window.__rerenders=0;renderView=()=>{window.__rerenders++;};
    window.__collects=0;collectServers=()=>{window.__collects++;};
  `);
  rt.run("svcTab('nfs')");
  assert.equal(rt.run("activeSvcTab"), "nfs", "tab 选中态是模块级全局（切服务器保持）");
  assert.equal(rt.run("window.__panes.map(p=>p.hidden).join()"), "true,true,false,true",
    "只有目标 pane 可见，其余 hidden");
  assert.equal(rt.run("window.__tabs[2].classList.contains('is-active')"), true);
  assert.equal(rt.run("window.__tabs[0].classList.contains('is-active')"), false);
  assert.equal(rt.run("window.__tabs[2].getAttribute('aria-selected')"), "true");
  // 历史守卫契约：切 tab 不重渲染（未保存表单存活）、不 collect、不碰 dirty
  assert.equal(rt.run("window.__addr.value"), "u@edited.example");
  assert.equal(rt.run("window.__rerenders"), 0);
  assert.equal(rt.run("window.__collects"), 0);
  assert.equal(rt.run("dirty"), false);
  rt.run("svcTab('conn')");
  assert.equal(rt.run("window.__panes[0].hidden"), false);
  assert.equal(rt.run("window.__addr.value"), "u@edited.example",
    "切走再切回，未保存的表单编辑必须还在（纯显隐，DOM 从未重建）");
  assert.equal(rt.run("window.__rerenders"), 0);
});

test("servers view follows the shared activeTunnel selection", () => {
  const rt = makeRuntime();
  rt.run(`
    S=normalizeState({mp:{servers:[
      {id:'t-1',name:'srv-a',ssh:{host:'a.example'},services:{nfs:{enabled:true,local_port:12049,mounts:[]}}},
      {id:'t-2',name:'srv-b',ssh:{host:'b.example'},services:{nfs:{enabled:false,local_port:12049,mounts:[]}}},
    ]}});
    activeTunnel=0;
  `);
  assert.match(rt.run("serversHTML()"),
    /is-selected" onclick="selectServer\(0\)"/);
  rt.run("renderView=()=>undefined;selectServer(1)");
  assert.equal(rt.run("activeTunnel"), 1);
  assert.match(rt.run("serversHTML()"),
    /is-selected" onclick="selectServer\(1\)"/);
  assert.doesNotMatch(rt.run("serversHTML()"),
    /is-selected" onclick="selectServer\(0\)"/);
});

test("svcCheck probes the saved server per card and renders probe results", async () => {
  const calls = [];
  const rt = makeRuntime(async (url, opts) => {
    const body = JSON.parse(opts.body);
    calls.push({ url, method: opts.method, body });
    const results = body.only === "ssh"
      ? { ssh: { ok: true, error: "", latency_ms: 87 } }
      : { openvpn: { ok: true, error: "", installed: false, version: "" } };
    return { ok: true, json: async () => ({ ok: true, results }) };
  });
  rt.run(`
    S=normalizeState({mp:{servers:[
      {id:'t-1',name:'srv-a',ssh:{user:'u',host:'a.example',port:22},
       services:{ssh:{forwards:[],autostart:false}}}]}});
    activeView='servers';activeTunnel=0;
  `);
  await rt.run(`
    (async()=>{
      const btn={textContent:'检测服务',disabled:false};
      await svcCheck('ssh',btn);
      await svcCheck('openvpn',btn);
      return btn;
    })()
  `);
  // SAVED-config 语义：按 index 探测已保存服务器，only 定向单卡
  assert.deepEqual(calls, [
    { url: "/api/server-check", method: "POST", body: { index: 0, only: "ssh" } },
    { url: "/api/server-check", method: "POST", body: { index: 0, only: "openvpn" } },
  ]);
  assert.equal(rt.elements.get("probe-ssh").textContent, "✅ SSH 可达 · 87ms");
  assert.equal(
    rt.elements.get("probe-vpn").textContent,
    "❌ 未安装（安装后此处将显示可用入口）");
});

test("svcCheck renders connection failure from the card probe", async () => {
  const rt = makeRuntime(async () => ({
    ok: true,
    json: async () => ({
      ok: true,
      results: { openvpn: { ok: false, error: "连接超时", installed: false, version: "" } },
    }),
  }));
  rt.run(`
    S=normalizeState({mp:{servers:[
      {id:'t-1',ssh:{host:'a.example',port:22},services:{ssh:{forwards:[]}}}]}});
    activeView='servers';activeTunnel=0;
  `);
  await rt.run(`
    (async()=>{
      const btn={textContent:'检测服务',disabled:false};
      await svcCheck('openvpn',btn);
      return btn;
    })()
  `);
  assert.equal(rt.elements.get("probe-vpn").textContent, "❌ 连接超时");
  assert.equal(rt.elements.get("probe-vpn").style.color, "var(--danger)");
});


test("collectServers reads per-row enabled switches", () => {
  const rt = makeRuntime();
  rt.run(`
    S=normalizeState({mp:{servers:[{name:'t1',
      ssh:{user:'u',host:'h',port:22,auth_type:'key',ssh_key:'',compression:true},
      services:{ssh:{forwards:[],autostart:false}}}]}});
    baselineState=cloneData(S);baselineRoles={};ccRoles={};
    activeView='servers';activeTunnel=0;recomputeDirty();
    const fields={name:{value:'t1'},addr:{value:'u@h'},ssh_port:{value:'22'},
      auth:{value:'key'},key:{value:''}};
    const row=(vals,sw)=>({querySelector:function(sel){
      const m=sel.match(/data-fwf="enabled"/);
      if(m)return sw||null;
      const k=sel.match(/data-fwf="(\\w+)"/);
      return k?vals[k[1]]:null;}});
    const rows=[row({local_port:{value:'9000'},remote_host:{value:'x'},
      remote_port:{value:'80'}},{getAttribute:()=>'true'}),
      row({local_port:{value:'9001'},remote_host:{value:'x'},
        remote_port:{value:'81'}},{getAttribute:()=>'false'})];
    window.__detail={querySelector:function(sel){
      const m=sel.match(/data-tf="(\\w+)"/);
      if(m&&fields[m[1]])return fields[m[1]];
      if(m&&m[1]==='compress')return{getAttribute:()=>'true'};
      return null;
    },querySelectorAll:function(sel){
      return sel.includes('data-fwr')?rows:[];
    }};
    document.querySelector=function(sel){
      return sel.includes('.md-detail')?window.__detail:null;
    };
  `);
  rt.run("collectServers();recomputeDirty()");
  assert.equal(rt.run("JSON.stringify(S.mp.servers[0].services.ssh.forwards)"),
    JSON.stringify([
      { local_port: 9000, remote_host: "x", remote_port: 80, enabled: true },
      { local_port: 9001, remote_host: "x", remote_port: 81, enabled: false },
    ]), "行内启用开关经 aria-checked 读回（逐条启停的配置面）");
  assert.equal(rt.run("dirty"), true);
});


test('vpn 函数体不得引用守卫收敛已删除的下标变量（2eada40 回归钉）', () => {
  const html = HTML;
  // 守卫收敛后 i 的声明只在 vpnTarget/removeServer 内——vpn 成功路径
  // 的表单回写必须走 tgt.row（曾因 S.mp.servers[i] 残留 ReferenceError）
  assert.ok(!/vpnNode\(S\.mp\.servers\[i\]\)/.test(html),
    'vpn 成功路径回写必须用 vpnTarget() 的 row 引用，不得用下标 i');
  assert.ok(/server_id:tgt\.server_id/.test(html) && /vpnNode\(tgt\.row\)/.test(html),
    'vpnTarget 寻址与回写两半必须同时在册');
});
