"""Offline protocol checks; no emulator connection or App activity."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

import ui_protocol


class PromptProtocolTests(unittest.TestCase):
    def test_terminal_call_still_uses_standard_input(self):
        with patch("builtins.input", return_value="已确认") as terminal:
            self.assertEqual(ui_protocol.choose("terminal prompt", [("继续", "已确认")]), "已确认")
        terminal.assert_called_once_with("terminal prompt")

    def test_web_prompt_publishes_buttons_and_uses_same_answer(self):
        output = io.StringIO()
        def callback():
            return ui_protocol.choose("输入 已确认", [("已就绪，继续", "已确认")], text="点此继续")
        with patch("sys.stdin", io.StringIO("已确认\n")), contextlib.redirect_stdout(output):
            self.assertEqual(ui_protocol.run(callback), "已确认")
        message = json.loads(output.getvalue())
        self.assertEqual(message["text"], "点此继续")
        self.assertEqual(message["control"], {"type": "choice", "choices": [{"label": "已就绪，继续", "value": "已确认"}]})
        self.assertIsNone(ui_protocol._event_sink)

    def test_click_continue_sends_blank_line_and_closed_connection_fails(self):
        for content, fails in (("\n", False), ("", True)):
            with patch("sys.stdin", io.StringIO(content)), contextlib.redirect_stdout(io.StringIO()):
                if fails:
                    with self.assertRaises(EOFError):
                        ui_protocol.run(lambda: ui_protocol.acknowledge("按回车"))
                else:
                    self.assertEqual(ui_protocol.run(lambda: ui_protocol.acknowledge("按回车")), "")

    def test_start_prompt_carries_required_app_mode_to_the_controller(self):
        output = io.StringIO()
        with patch("sys.stdin",io.StringIO("\n")),contextlib.redirect_stdout(output):
            ui_protocol.run(lambda:ui_protocol.acknowledge("倒计时结束",app_run_mode="campus"))
        message=json.loads(output.getvalue())
        self.assertEqual(message["control"]["requires_app_run_mode"],"campus")

    def test_home_prompt_carries_home_requirement_only_when_requested(self):
        for required in (False, True):
            with self.subTest(required=required):
                output = io.StringIO()
                with patch("sys.stdin", io.StringIO("\n")), contextlib.redirect_stdout(output):
                    ui_protocol.run(lambda: ui_protocol.acknowledge("开始预热", requires_app_home=required))
                control = json.loads(output.getvalue())["control"]
                if required:
                    self.assertIs(control["requires_app_home"], True)
                else:
                    self.assertNotIn("requires_app_home", control)
                self.assertNotIn("requires_app_run_mode", control)


if __name__ == "__main__":
    unittest.main()
