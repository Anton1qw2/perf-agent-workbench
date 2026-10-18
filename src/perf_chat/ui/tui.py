import asyncio
import json
import os

from dotenv import load_dotenv
from rich.markdown import Markdown
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Header, Input, RichLog, Static

from .client import ApiClient, request_id


class PerformanceChat(App):
    TITLE = "Performance Chat"
    CSS = """
    #chat { width: 1fr; border: round #475569; }
    #spec { width: 35; border: round #475569; overflow-y: auto; display: none; }
    #status { height: 3; padding: 1; }
    Input { dock: bottom; }
    """
    BINDINGS = [("ctrl+q", "quit", "Quit"), ("f2", "toggle_context", "Settings")]

    def __init__(self, url, cid=None):
        super().__init__()
        self.api = ApiClient(url)
        self.cid = cid
        self.state = None
        self.listener = None
        self.refresh_generation = 0

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Connecting…", id="status", markup=False)
        with Horizontal():
            yield RichLog(id="chat", wrap=True, markup=False)
            yield Static("", id="spec", markup=False)
        yield Input(placeholder="Message · /run · /cancel · /new · /open ID · /set field=value")
        yield Footer()

    async def on_mount(self):
        self.run_worker(self.connect(), exclusive=True, group="connection")

    async def connect(self):
        try:
            if not self.cid:
                recent = await self.api.call("GET", "/conversations")
                self.cid = (
                    recent[0]["id"]
                    if recent
                    else (await self.api.call("POST", "/conversations"))["id"]
                )
            await self.refresh_state()
            self.listener = self.run_worker(self.listen(), exclusive=True, group="events")
            self.query_one(Input).focus()
        except Exception as exc:
            self.query_one("#status", Static).update(f"Error: {exc}")

    async def refresh_state(self):
        current = self.cid
        self.refresh_generation += 1
        generation = self.refresh_generation
        state = await self.api.call("GET", f"/conversations/{current}")
        if current != self.cid or generation != self.refresh_generation:
            return
        self.state = state
        self.render_state()

    def render_state(self):
        log = self.query_one("#chat", RichLog)
        log.clear()
        for message in self.state["messages"]:
            role = {"user": "You: ", "executor": "Executor: "}.get(message["role"], "Agent: ")
            log.write(role)
            log.write(Markdown(message["content"], hyperlinks=False))
        runs = self.state["runs"]
        run = runs[0] if runs else None
        if run and run.get("progress") and not run.get("result"):
            log.write("Executor: " + run["progress"])
        if self.state.get("last_error"):
            log.write(self.state["last_error"]["message"])
        context = self.state["spec"]["target"]
        credentials = self.state.get("credentials", {})
        self.query_one("#spec", Static).update(
            "Repository\n\n"
            f"URL / alias: {context.get('repository') or '—'}\n\n"
            f"Branch: {context.get('branch') or '—'}\n\n"
            f"GitLab token: {'Saved' if credentials.get('git_token_configured') else 'Not configured'}\n\n"
            "Profiler\n\n"
            f"Result ID: {context.get('profile') or '—'}\n\n"
            f"API token: {'Saved' if credentials.get('profiler_token_configured') else 'Not configured'}\n\n"
            "Manage tokens through project settings in Web."
        )
        self.query_one("#status", Static).update(
            f"Chat {self.cid} | v{self.state['spec']['spec_version']} | "
            + ("agent is replying" if self.state["busy"] else (run["status"] if run else "ready"))
        )

    def action_toggle_context(self):
        panel = self.query_one("#spec", Static)
        panel.display = not panel.display

    async def listen(self):
        cursor = 0
        while True:
            try:
                await self.refresh_state()
                async for event in self.api.events(self.cid, cursor):
                    await self.refresh_state()
                    cursor = event["id"]
            except asyncio.CancelledError:
                raise
            except Exception:
                self.query_one("#status", Static).update("Connection lost; reconnecting…")
                await asyncio.sleep(2)

    async def on_input_submitted(self, event: Input.Submitted):
        text = event.value.strip()
        if not text:
            return
        event.input.disabled = True
        try:
            await self.command(text)
            event.input.value = ""
            await self.refresh_state()
        except Exception as exc:
            self.query_one("#chat", RichLog).write(f"Error: {exc}")
        finally:
            event.input.disabled = False
            event.input.focus()

    async def command(self, text):
        if text == "/new" or text.startswith("/open "):
            candidate = (
                text[6:].strip()
                if text.startswith("/open ")
                else (await self.api.call("POST", "/conversations"))["id"]
            )
            candidate_state = await self.api.call("GET", f"/conversations/{candidate}")
            if self.listener:
                self.listener.cancel()
            self.refresh_generation += 1
            self.cid = candidate
            self.state = candidate_state
            self.render_state()
            self.listener = self.run_worker(self.listen(), exclusive=True, group="events")
            return
        if not self.state:
            raise ValueError("Not connected to the API")
        path = f"/conversations/{self.cid}"
        run = self.state["runs"][0] if self.state["runs"] else None
        if text == "/run":
            await self.api.call(
                "POST",
                path + "/runs",
                {
                    "spec_version": self.state["spec"]["spec_version"],
                    "request_id": request_id(),
                },
            )
        elif text == "/cancel":
            if not run:
                raise ValueError("No run available")
            await self.api.call("POST", f"/runs/{run['id']}/cancel")
        elif text.startswith("/set "):
            spec = json.loads(json.dumps(self.state["spec"]))
            key, value = text[5:].split("=", 1)
            if key.strip() not in {"target.repository", "target.branch", "target.profile"}:
                raise ValueError(
                    "Use /set for repository, branch and profile; clarify the task in chat"
                )
            node = spec
            parts = key.strip().split(".")
            for part in parts[:-1]:
                node = node[part]
            node[parts[-1]] = None if value.strip() == "null" else value.strip()
            await self.api.call(
                "PUT",
                path + "/spec",
                {
                    "expected_version": spec["spec_version"],
                    "spec": spec,
                },
            )
        elif run and run["status"] == "waiting":
            await self.api.call(
                "POST",
                f"/runs/{run['id']}/resume",
                {
                    "request_id": run["clarification"]["request_id"],
                    "answer": text,
                },
            )
        else:
            await self.api.call(
                "POST",
                path + "/messages",
                {
                    "content": text,
                    "request_id": request_id(),
                },
            )


def main():
    import argparse

    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.getenv("PERF_CHAT_API_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--conversation")
    args = parser.parse_args()
    PerformanceChat(args.url, args.conversation).run()


if __name__ == "__main__":
    main()
