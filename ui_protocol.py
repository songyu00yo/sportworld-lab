"""Line-delimited UI messages for the existing interactive CLI subprocess."""
import builtins
import json
import sys

_event_sink = None


def web_active():
    return _event_sink is not None


def ask(prompt, *, text=None, control=None):
    """Keep terminal input compatible while exposing explicit clickable UI controls."""
    if _event_sink is None:
        return builtins.input(prompt)
    sys.stdout.flush()
    _event_sink({"kind": "prompt", "text": text or prompt.strip(), "control": control})
    answer = sys.stdin.readline()
    if not answer:
        raise EOFError("控制面板输入连接已关闭")
    return answer.rstrip("\r\n")


def choose(prompt, choices, *, text=None):
    options = [{"label": label, "value": value} for label, value in choices]
    return ask(prompt, text=text, control={"type": "choice", "choices": options})


def acknowledge(prompt, label="已完成，继续", *, text=None, app_run_mode=None, requires_app_home=False):
    control = {"type": "choice", "choices": [{"label": label, "value": ""}]}
    if app_run_mode is not None:
        control["requires_app_run_mode"] = app_run_mode
    if requires_app_home:
        control["requires_app_home"] = True
    return ask(prompt, text=text, control=control)


def publish_progress(data):
    if _event_sink is not None:
        _event_sink({"kind": "telemetry", "data": data})


def publish_plan(data):
    if _event_sink is not None:
        _event_sink({"kind": "plan", "data": data})


def run(callback):
    global _event_sink
    output, error_output, original_input = sys.stdout, sys.stderr, builtins.input

    def event(message):
        output.write(json.dumps(message, ensure_ascii=False)+"\n")
        output.flush()

    def emit(kind, text):
        event({"kind": kind, "text": text})

    class Messages:
        def __init__(self):
            self.buffer = ""
        def write(self, text):
            self.buffer += text
            while "\n" in self.buffer:
                line, self.buffer = self.buffer.split("\n", 1)
                if line:
                    emit("output", line)
            return len(text)
        def flush(self):
            if self.buffer:
                emit("output", self.buffer)
                self.buffer = ""

    messages = Messages()
    _event_sink = event
    sys.stdout = sys.stderr = messages
    builtins.input = lambda prompt="": ask(prompt)
    try:
        return callback()
    except KeyboardInterrupt:
        emit("output", "已中止准备流程。")
        return 130
    finally:
        messages.flush()
        _event_sink = None
        sys.stdout, sys.stderr, builtins.input = output, error_output, original_input
