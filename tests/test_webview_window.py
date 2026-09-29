"""Native copy acknowledgements cross the real ObjC adapter and app intent."""
import json
from unittest.mock import MagicMock, patch

from shellui import webview_window
from tests.test_menu_actions import _make_app


def test_copy_result_reaches_originating_settings_page_without_instruction_text():
    app = _make_app()
    app._config_server = MagicMock()
    app._config_server.agent_instructions.return_value = "private instruction token"
    delegate = webview_window._ConfigWindowDelegate.alloc().init()
    delegate.setActionHandler_(app._bridge_action)
    message = MagicMock()
    message.name.return_value = "bridge"
    message.body.return_value = {"type": "copyAgentInstructions", "payload": {}}
    with patch.object(webview_window, "_webview") as view, \
            patch("services.intents.subprocess.run"), patch.object(app, "_notify"):
        delegate.userContentController_didReceiveScriptMessage_(None, message)
    js, completion = view.evaluateJavaScript_completionHandler_.call_args.args
    payload = json.loads(js[len("window.__native&&window.__native.receive("):-1])
    assert payload == {"type": "agentInstructionsCopied", "payload": {"ok": True}}
    assert "private instruction token" not in js
    assert completion is None


def test_native_copy_exception_returns_failure_to_page():
    delegate = webview_window._ConfigWindowDelegate.alloc().init()
    delegate.setActionHandler_(MagicMock(side_effect=RuntimeError("failed")))
    message = MagicMock()
    message.name.return_value = "bridge"
    message.body.return_value = {"type": "copyAgentInstructions", "payload": {}}
    with patch.object(webview_window, "_webview") as view:
        delegate.userContentController_didReceiveScriptMessage_(None, message)
    js = view.evaluateJavaScript_completionHandler_.call_args.args[0]
    payload = json.loads(js[len("window.__native&&window.__native.receive("):-1])
    assert payload["payload"]["ok"] is False
    assert payload["payload"]["error"]
