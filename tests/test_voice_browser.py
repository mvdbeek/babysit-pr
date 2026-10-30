"""Voice assistant: spoken and typed requests, answered by a stand-in for the supervisor's
Claude, and the dashboard commands its answers run."""

import json
import re

import pytest
import voice_assistant
from playwright.sync_api import Page, expect
from test_dashboard_browser import dashboard_site as dashboard_site
from test_dashboard_browser import viewer_routes as viewer_routes
from test_dashboard_browser import workspace_routes as workspace_routes
from test_workspace_overview_browser import site as site

pytestmark = pytest.mark.browser

# A recognizer that hears what the test says, and a speech synth that records replies.
FAKE_SPEECH = """
window.voiceSessions = [];
window.spoken = [];
class FakeRecognition {
  constructor() { window.voiceSessions.push(this); this.running = false; }
  start() { this.running = true; window.voiceSession = this; setTimeout(() => this.onstart?.()); }
  stop() {
    if (!this.running) return;
    this.running = false;
    setTimeout(() => this.onend?.());
  }
  abort() { this.stop(); }
}
window.SpeechRecognition = FakeRecognition;
// Replies are spoken only where a test turns that on: a reply mutes the microphone briefly.
if (!localStorage.getItem("babysit-pr:voice:v1"))
  localStorage.setItem("babysit-pr:voice:v1", JSON.stringify({ speak: false }));
// A continuous session: results pile up, and words still forming are replaced as they firm up.
window.hear = (texts, isFinal = true) => {
  const session = window.voiceSession;
  const alternatives = [].concat(texts).map((transcript) => ({ transcript, confidence: 0.9 }));
  const results = (session.results ||= []);
  if (results.length && !results[results.length - 1].isFinal) results.pop();
  results.push(Object.assign(alternatives, { isFinal }));
  session.onresult({ resultIndex: results.length - 1, results: [...results] });
};
Object.defineProperty(window, "speechSynthesis", {
  configurable: true,
  value: {
    speaking: false,
    cancel() {},
    // The empty utterance is the one a tap speaks so that iOS allows speech later.
    speak(utterance) {
      if (utterance.text) window.spoken.push(utterance.text);
      setTimeout(() => utterance.onend?.());
    },
  },
});
"""
NO_SPEECH = """
delete window.SpeechRecognition;
delete window.webkitSpeechRecognition;
Object.defineProperty(window, "webkitSpeechRecognition", { value: undefined });
"""


def open_voice(page: Page, url: str, path: str = "/"):
    page.add_init_script(FAKE_SPEECH)
    page.goto(url + path)
    expect(page.locator("#updated")).to_contain_text("Refreshed")
    page.locator("#voice-toggle").click()
    expect(page.locator("#voice-bar")).to_be_visible()
    expect(page.locator("#voice-mic")).to_have_attribute("aria-pressed", "true")


THINKING = re.compile(r"\bvoice-thinking\b")
SETTLING = re.compile(r"\bvoice-settling\b")


def answered(page: Page):
    """Waits for the assistant's answer: a newer request would overtake one still pending."""
    expect(page.locator("#voice-bar")).not_to_have_class(THINKING)


def hear(page: Page, *texts: str, interim: bool = False, settle: bool = True):
    """Say something once listening, as the recognizer would report it; `settle` waits
    for the pause after it to run out."""
    # Waits in the page: wait_for_function evaluates strings, which the dashboard's CSP forbids.
    page.evaluate(
        """async ([texts, final]) => {
            for (let i = 0; i < 300 && !window.voiceSession?.running; i++)
                await new Promise((resolve) => setTimeout(resolve, 10));
            window.hear(texts, final);
        }""",
        [list(texts), not interim],
    )
    if settle and not interim:
        # Sent once the speaker has paused.
        expect(page.locator("#voice-bar")).not_to_have_class(SETTLING, timeout=10000)
        answered(page)


def command(page: Page, text: str, wait: bool = True):
    """Type a request into the bar, as on a browser without speech recognition."""
    page.locator("#voice-input").fill(text)
    page.locator("#voice-input").press("Enter")
    if wait:
        answered(page)


def eventually(page: Page, check, seconds: float = 5):
    for _ in range(int(seconds * 20)):
        if check():
            return
        page.wait_for_timeout(50)
    assert check()


class FakeAssistant:
    """Answers /api/voice-turn in place of the supervisor's Claude.

    A request with a scripted reply gets it; any other is run as the dashboard command
    it names, so the tests can drive every command through the assistant's own path.
    """

    def __init__(self, page: Page):
        self.requests: list[dict] = []
        self.cancelled: list[str] = []
        self.replies: dict[str, dict] = {}
        self.progress: list[str] = []
        self.held = False
        self.error: str | None = None
        page.route("**/api/voice-turn**", self.handle)

    def reply(self, text: str, say: str = "", *actions: dict):
        self.replies[text] = {"say": say, "actions": list(actions)}

    def last(self):
        return self.requests[-1]

    def handle(self, route):
        request = route.request
        if request.method == "POST":
            body = request.post_data_json
            if "cancel" in body:
                self.cancelled.append(body["cancel"])
                route.fulfill(json={"cancelled": True})
                return
            if self.error:
                route.fulfill(status=400, json={"error": self.error})
                return
            self.requests.append(body)
            route.fulfill(json={"id": f"{len(self.requests):032x}"})
            return
        if self.held:
            route.fulfill(json={"status": "running", "progress": self.progress, "reply": None})
            return
        text = self.requests[-1]["text"]
        reply = self.replies.get(text) or {
            "say": "",
            "actions": [{"type": "command", "text": text}],
        }
        route.fulfill(json={"status": "done", "progress": [], "reply": reply, "error": None})


@pytest.fixture
def assistant(page: Page):
    return FakeAssistant(page)


def test_a_spoken_request_goes_to_the_assistant_and_the_conversation_goes_on(
    page: Page, dashboard_site, assistant
):
    url, _ = dashboard_site
    first = (
        "Two things need you. First, test beta pull request 9 is assigned to you. "
        "Shall I start a review, or move on?"
    )
    assistant.reply(
        "Walk me through what needs me",
        first,
        {"type": "open", "url": "https://github.com/test/beta/pull/9", "page": "prs"},
    )
    assistant.reply("next", "Second, alpha issue 30 crashes on start.")
    open_voice(page, url)
    page.locator("#voice-help-toggle").click()
    page.locator("#voice-speak").check()
    hear(page, "walk me through", interim=True)
    expect(page.locator("#voice-heard")).to_have_text("“walk me through”")
    expect(page.locator("#voice-heard")).to_have_class("voice-interim")
    hear(page, "Walk me through what needs me")
    status = page.locator("#voice-status")
    expect(status).to_have_text(first)
    expect(page.locator("#pr-list tr", has_text="Assigned PR")).to_be_focused()
    request = assistant.requests[0]
    assert request["text"] == "Walk me through what needs me"
    assert request["history"] == []
    assert request["context"]["page"] == "attention"
    assert request["context"]["agent"] is None
    assert page.evaluate("window.spoken") == [first]
    # Once it has spoken, the bar listens for the answer without another press.
    expect(page.locator("#voice-mic")).to_have_attribute("aria-pressed", "true")
    # The microphone ignores the room for a moment after a reply, its echo included.
    page.wait_for_timeout(1100)
    hear(page, "next")
    expect(status).to_have_text("Second, alpha issue 30 crashes on start.")
    request = assistant.requests[1]
    assert request["history"] == [
        {"role": "user", "text": "Walk me through what needs me", "outcomes": []},
        {
            "role": "assistant",
            "text": first,
            "outcomes": ["open https://github.com/test/beta/pull/9 → done"],
        },
    ]
    assert request["context"]["page"] == "prs"
    assert request["context"]["item"] == "https://github.com/test/beta/pull/9"
    # Starting over forgets the conversation; it is the bar's, not the assistant's.
    page.wait_for_timeout(1100)
    hear(page, "start over")
    expect(status).to_have_text("Starting over.")
    command(page, "next")
    expect(status).to_have_text("Second, alpha issue 30 crashes on start.")
    assert assistant.last()["history"] == []
    assert len(assistant.requests) == 3


def test_the_bar_shows_progress_and_why_the_assistant_could_not_answer(
    page: Page, dashboard_site, assistant
):
    url, _ = dashboard_site
    open_voice(page, url)
    status = page.locator("#voice-status")
    assistant.held = True
    assistant.progress = ["Checking what needs you…", "Reading the CI failure…"]
    command(page, "why is CI failing", wait=False)
    expect(status).to_have_text("Reading the CI failure…")
    expect(page.locator("#voice-bar")).to_have_class(THINKING)
    assistant.reply("why is CI failing", "The lint job fails on an unused import.")
    assistant.held = False
    expect(status).to_have_text("The lint job fails on an unused import.")
    expect(page.locator("#voice-bar")).not_to_have_class(THINKING)
    assistant.error = "The watcher's supervisor is not running, so the assistant cannot answer."
    command(page, "hello")
    expect(status).to_have_text(
        "The assistant could not answer: The watcher's supervisor is not running, "
        "so the assistant cannot answer."
    )
    expect(status).to_have_class("voice-problem")
    # A request that got no answer is not part of the conversation.
    assistant.error = None
    assistant.reply("hi", "Hello.")
    command(page, "hi")
    expect(status).to_have_text("Hello.")
    assert [turn["text"] for turn in assistant.last()["history"]] == [
        "why is CI failing",
        "The lint job fails on an unused import.",
    ]


def test_the_assistants_commands_switch_tabs_and_say_why_they_failed(
    page: Page, dashboard_site, assistant
):
    url, _ = dashboard_site
    open_voice(page, url)
    status = page.locator("#voice-status")
    hear(page, "Go to pull requests.")
    expect(page.locator("#prs-panel")).to_be_visible()
    command(page, "show me the watcher tab")
    expect(page.locator("#watcher-panel")).to_be_visible()
    command(page, "back")
    expect(page.locator("#prs-panel")).to_be_visible()
    command(page, "cron jobs")
    expect(status).to_have_text("That did not work: Cron jobs is not set up on this dashboard.")
    expect(status).to_have_class("voice-problem")
    command(page, "PR nine")
    expect(page.locator("#pr-list tr", has_text="Assigned PR")).to_be_focused()
    command(page, "issue 30 in alpha")
    expect(page.locator("#issue-list tr", has_text="Crash on start")).to_be_focused()
    command(page, "PR 77")
    expect(status).to_have_text("That did not work: PR 77 is not in the dashboard.")
    command(page, "make me a sandwich")
    expect(status).to_have_text(
        "That did not work: There is no dashboard command “make me a sandwich”."
    )
    # The assistant hears next turn what each of its actions did.
    outcomes = [o for turn in assistant.last()["history"] for o in turn["outcomes"]]
    assert "command “PR 77” → failed: PR 77 is not in the dashboard." in outcomes
    assert "command “PR nine” → done: Showing test/beta #9." in outcomes


def test_lists_are_searched_filtered_and_sorted(page: Page, dashboard_site, assistant):
    url, _ = dashboard_site
    open_voice(page, url, "/#prs")
    rows = page.locator("#pr-list tr")
    expect(rows).to_have_count(2)
    command(page, "filter CI to failing")
    expect(page.locator("#pr-ci")).to_have_value("FAILURE")
    expect(rows).to_have_count(1)
    command(page, "clear filters")
    expect(rows).to_have_count(2)
    command(page, "filter by passed")
    expect(page.locator("#pr-ci")).to_have_value("SUCCESS")
    command(page, "clear filters")
    command(page, "search for assigned")
    expect(page.locator("#pr-search")).to_have_value("assigned")
    expect(rows).to_have_count(1)
    command(page, "clear search")
    expect(rows).to_have_count(2)
    command(page, "sort by author")
    expect(page.locator("#pr-sort")).to_have_value("author")
    heading = page.locator("#pr-heading-author")
    expect(heading).to_have_attribute("aria-sort", "ascending")
    command(page, "reverse order")
    expect(heading).to_have_attribute("aria-sort", "descending")


def test_an_ambiguous_button_is_named_back_and_a_row_picks_one(
    page: Page, dashboard_site, viewer_routes, assistant
):
    url, _ = dashboard_site
    open_voice(page, url, "/#prs")
    expect(
        page.locator("#pr-list tr").first.get_by_role("button", name="Transcript")
    ).to_be_visible()
    command(page, "transcript")
    status = page.locator("#voice-status")
    expect(status).to_contain_text("That did not work: 2 controls match “transcript”")
    expect(status).to_contain_text("Transcript for #9")
    command(page, "transcript for beta 9")
    viewer = page.locator("#ws-viewer")
    expect(viewer).to_be_visible()
    expect(page.locator("#ws-viewer-links")).to_contain_text("PR #9")
    # Everything outside the modal viewer is inert, so the bar moves into it.
    expect(viewer.locator("#voice-bar")).to_be_visible()
    command(page, "close")
    expect(viewer).to_be_hidden()
    expect(page.locator("body > #voice-bar")).to_be_visible()
    command(page, "diff for 8")
    expect(viewer).to_be_visible()
    expect(page.locator("#ws-viewer-links")).to_contain_text("PR #8")
    # Only a real row: a number elsewhere on the page names none.
    command(page, "close")
    command(page, "details for 31")
    expect(status).to_contain_text("There is no dashboard command")


def test_a_message_to_an_agent_is_sent_only_once_the_user_confirms(
    page: Page, dashboard_site, viewer_routes, assistant
):
    url, _ = dashboard_site
    say = "I'll ask the agent to rebase on main."
    # What is confirmed is read from the page, whatever the assistant says.
    prompt = (
        "Say “confirm” to send “Rebase on main, then rerun the tests.” to workspace w1, "
        "or “cancel”."
    )
    message = {
        "type": "message_agent",
        "workspace": "w1",
        "name": "test/beta #9",
        "text": "Rebase on main, then rerun the tests.",
    }
    assistant.reply("tell it to rebase", say, message)
    assistant.reply("what is it doing", "It is fixing the crash.")
    open_voice(page, url)
    command(page, "tell it to rebase")
    viewer = page.locator("#ws-viewer")
    expect(viewer).to_be_visible()
    # Named as the dashboard knows it (here, by its id alone), never by the assistant.
    expect(page.locator("#ws-viewer-title")).to_have_text("workspace w1")
    box = viewer.get_by_label("Message the agent")
    expect(box).to_have_value("Rebase on main, then rerun the tests.")
    send = viewer.get_by_role("button", name="Send", exact=True)
    expect(send).to_have_class(re.compile(r"\bvoice-pending\b"))
    expect(page.locator("#voice-status")).to_have_text(f"{say} {prompt}")
    # Anything else first leaves it unsent, the user's own draft back, and the assistant
    # hears so.
    command(page, "what is it doing")
    expect(page.locator("#voice-status")).to_have_text("It is fixing the crash.")
    expect(send).not_to_have_class(re.compile(r"\bvoice-pending\b"))
    expect(box).to_have_value("")
    assert viewer_routes["sent"] == []
    assert assistant.last()["history"][1]["outcomes"] == [
        "message test/beta #9: “Rebase on main, then rerun the tests.” → "
        "not confirmed; the user went on to something else"
    ]
    assert assistant.last()["context"]["agent"] == {
        "workspace": "w1",
        "name": "workspace w1",
        "mode": "transcript",
    }
    command(page, "tell it to rebase")
    expect(send).to_have_class(re.compile(r"\bvoice-pending\b"))
    command(page, "confirm")
    expect(page.locator("#ws-message-status")).to_have_text("Sent to the agent in w1:p1.")
    assert viewer_routes["sent"][0][1]["text"] == "Rebase on main, then rerun the tests."
    expect(page.locator("#voice-status")).to_have_text("Sent.")
    command(page, "what is it doing")
    expect(page.locator("#voice-status")).to_have_text("It is fixing the crash.")
    assert assistant.last()["history"][-1]["outcomes"] == [
        "message test/beta #9: “Rebase on main, then rerun the tests.” → confirmed: Sent."
    ]


def test_a_message_that_cannot_be_sent_says_why(
    page: Page, dashboard_site, viewer_routes, assistant
):
    url, _ = dashboard_site
    viewer_routes["agents"] = []
    viewer_routes["sessions"] = []
    message = {"type": "message_agent", "workspace": "w1", "name": "w1", "text": "Re-review."}
    assistant.reply("send re-review to the agent", "I'll ask it to re-review.", message)
    open_voice(page, url)
    command(page, "send re-review to the agent")
    expect(page.locator("#voice-status")).to_contain_text(
        "The message cannot be sent: No agent is running and no session was recorded here."
    )
    assert viewer_routes["sent"] == []


def test_an_agents_question_is_answered_only_once_the_user_confirms(
    page: Page, dashboard_site, viewer_routes, assistant
):
    url, _ = dashboard_site
    agent = viewer_routes["agents"][1]
    agent.update(
        status="blocked",
        session="live-session",
        interaction={
            "screen": "",
            "question": None,
            "idle": False,
            "choices": {
                "id": "dialog:abc",
                "text": "Do you want to proceed?",
                "options": [{"key": "1", "label": "Yes"}, {"key": "2", "label": "No"}],
            },
        },
    )
    viewer_routes["agents"] = [agent]
    chosen = []

    def choose(route):
        chosen.append(route.request.post_data_json)
        route.fulfill(json={"chosen": True, "pane": "w1:p2"})

    page.route("**/api/workspace-choose", choose)
    open_voice(page, url, "/#prs")
    page.locator("#pr-list tr").first.get_by_role("button", name="Transcript", exact=True).click()
    interaction = page.locator("#ws-agent-interaction")
    expect(interaction.get_by_role("button", name="Yes", exact=True)).to_be_visible()
    assistant.reply("let it go ahead", "I'll answer Yes.", {"type": "command", "text": "option 1"})
    command(page, "let it go ahead")
    expect(page.locator("#voice-status")).to_have_text(
        "I'll answer Yes. Say “confirm” to press Yes, or “cancel”."
    )
    # The assistant saw the question and its choices.
    assert assistant.last()["context"]["agent_question"]["options"] == ["Yes", "No"]
    command(page, "cancel")
    expect(page.locator("#voice-status")).to_have_text("Cancelled.")
    assert chosen == []
    # Only the user confirms: "yes" with nothing waiting goes to the assistant, and its
    # own "confirm" is no command.
    assistant.reply("yes", "", {"type": "command", "text": "confirm"})
    command(page, "yes")
    expect(page.locator("#voice-status")).to_contain_text("That did not work")
    assert chosen == []
    assistant.reply("say no instead", "I'll answer No.", {"type": "command", "text": "option 2"})
    command(page, "say no instead")
    command(page, "confirm")
    expect(interaction.locator(".ws-answer-status")).to_contain_text("Chose “No”")
    assert chosen[0]["option"] == "2"


def test_presses_that_are_hard_to_undo_wait_for_confirmation(page: Page, dashboard_site, assistant):
    url, _ = dashboard_site
    open_voice(page, url)
    status = page.locator("#voice-status")
    command(page, "settings")
    dialog = page.locator("#settings-dialog")
    expect(dialog).to_be_visible()
    prompt = dialog.locator("#settings-prompt")
    expect(prompt).not_to_have_value("")
    prompt.fill("A custom prompt {url}")
    # Saving changes what every device uses: it waits for confirmation too.
    command(page, "save prompt")
    expect(status).to_have_text(
        "Say “confirm” to press Save prompt with “A custom prompt {url}”, or “cancel”."
    )
    expect(dialog.get_by_role("button", name="Save prompt")).to_have_class("voice-pending")
    command(page, "yes")
    # Use the default only takes once the saved prompt differs from it.
    expect(dialog.get_by_role("button", name="Use the default")).to_be_enabled()
    command(page, "use the default")
    expect(status).to_contain_text("Say “confirm” to press Use the default")
    command(page, "no")
    expect(status).to_have_text("Cancelled.")
    expect(prompt).to_have_value("A custom prompt {url}")
    command(page, "use the default")
    command(page, "confirm")
    expect(prompt).not_to_have_value("A custom prompt {url}")
    # A setting saves for every device as it changes, through its searchable picker.
    reviewer = dialog.locator("#settings-reviewer")
    before = reviewer.input_value()
    after = "claude" if before == "codex" else "codex"
    command(page, f"set main reviewer to {after}")
    expect(status).to_contain_text("Say “confirm” to set")
    expect(dialog.locator(".searchable-select.voice-pending")).to_have_count(1)
    assert reviewer.input_value() == before
    command(page, "confirm")
    expect(reviewer).to_have_value(after)
    expect(dialog.locator(".voice-pending")).to_have_count(0)


def test_ready_workspaces_are_cleaned_up_once_the_user_confirms(page: Page, site, assistant):
    url, _, removed = site
    request = "Clean up the workspaces that are ready"
    assistant.reply(
        request,
        "I'll clean up the workspaces that are ready.",
        {"type": "open", "page": "workspaces"},
        {"type": "command", "text": "filter status to ready to clean up"},
        {"type": "command", "text": "check select all shown"},
        {"type": "command", "text": "click clean up selected"},
        {"type": "command", "text": "click clean up workspaces"},
    )
    open_voice(page, url, "/#workspaces")
    command(page, request)
    # The controls on screen went with the request, named as commands name them.
    controls = assistant.last()["context"]["controls"]
    assert "Select all shown" in controls["buttons"]
    status = next(
        entry
        for entry in controls["lists"]
        if entry["name"] == "Filter workspaces by cleanup status"
    )
    assert status["value"] == "All workspaces" and "Ready to clean up" in status["options"]
    # Opening the review dialog changes nothing; cleaning up waits for confirmation.
    dialog = page.locator("#ws-dialog")
    expect(dialog).to_be_visible()
    expect(page.locator("#voice-status")).to_contain_text(
        "Say “confirm” to press Clean up 1 workspace(s), or “cancel”."
    )
    assert removed == []
    command(page, "confirm")
    expect(dialog).to_contain_text("Cleanup results")
    assert [target["key"] for target in removed] == ["/src/worktrees/repo/merged-work"]


def test_a_large_page_is_described_within_the_servers_limit(page: Page, dashboard_site):
    url, _ = dashboard_site
    page.goto(url)
    described = page.evaluate(
        """() => window.dashboardVoice.within({
            page: "prs",
            controls: {
                buttons: ["Refresh"],
                row_buttons: Array.from({ length: 20 }, (_, n) => `Action ${n} `.repeat(6)),
                lists: Array.from({ length: 12 }, (_, n) => ({
                    name: `List ${n}`,
                    value: "All",
                    options: Array.from({ length: 20 }, (_, m) => `Option ${m} `.repeat(5)),
                })),
            },
        }, 7500)"""
    )
    # Long option lists shrink first, then the rows' buttons; what is on screen stays.
    assert len(json.dumps(described, ensure_ascii=False)) <= 7500
    assert described["controls"]["buttons"] == ["Refresh"]
    assert all(len(entry["options"]) <= 6 for entry in described["controls"]["lists"])


# Speech that goes on until it is cancelled, as a long reply would.
HELD_SPEECH = """
Object.defineProperty(window, "speechSynthesis", {
  configurable: true,
  value: {
    speaking: false,
    current: null,
    cancel() {
      const utterance = this.current;
      this.current = null;
      this.speaking = false;
      if (utterance) setTimeout(() => utterance.onerror?.({ error: "interrupted" }));
    },
    speak(utterance) {
      if (!utterance.text) return;
      window.spoken.push(utterance.text);
      this.current = utterance;
      this.speaking = true;
    },
  },
});
"""


def test_saying_stop_cuts_a_reply_short_and_the_conversation_goes_on(
    page: Page, dashboard_site, assistant
):
    url, _ = dashboard_site
    reply = "Galaxy pull request 9 waits on two reviews, and its tests are still running."
    assistant.reply("what needs me", reply)
    assistant.reply("next one", "Nothing else.")
    page.add_init_script(FAKE_SPEECH)
    page.add_init_script(HELD_SPEECH)
    page.add_init_script('localStorage.setItem("babysit-pr:voice:v1", \'{"speak": true}\')')
    page.goto(url)
    expect(page.locator("#updated")).to_contain_text("Refreshed")
    page.locator("#voice-toggle").click()
    hear(page, "what needs me")
    status = page.locator("#voice-status")
    expect(status).to_have_text(reply)
    eventually(page, lambda: page.evaluate("window.speechSynthesis.speaking"))
    # The microphone hears the reply too: a word of it never stops it.
    hear(page, "its tests are still running", interim=True)
    assert page.evaluate("window.speechSynthesis.speaking")
    hear(page, "tests are still running stop", interim=True)
    eventually(page, lambda: not page.evaluate("window.speechSynthesis.speaking"))
    expect(status).to_have_text("Stopped reading.")
    # The same "stop", finished, neither stops listening nor goes to the assistant.
    hear(page, "stop")
    expect(page.locator("#voice-mic")).to_have_attribute("aria-pressed", "true")
    assert len(assistant.requests) == 1
    # The microphone ignores the room for a moment after a reply, as after any reading.
    page.wait_for_timeout(1100)
    hear(page, "next one")
    expect(status).to_have_text("Nothing else.")


def test_talking_over_a_reply_stops_it_and_is_the_next_request(
    page: Page, dashboard_site, assistant
):
    url, _ = dashboard_site
    reply = "Galaxy pull request 9 waits on two reviews, and its tests are still running."
    assistant.reply("what needs me", reply)
    assistant.reply("what about issue 30", "It crashes on start.")
    page.add_init_script(FAKE_SPEECH)
    page.add_init_script(HELD_SPEECH)
    page.add_init_script('localStorage.setItem("babysit-pr:voice:v1", \'{"speak": true}\')')
    page.goto(url)
    expect(page.locator("#updated")).to_contain_text("Refreshed")
    page.locator("#voice-toggle").click()
    hear(page, "what needs me")
    expect(page.locator("#voice-status")).to_have_text(reply)
    eventually(page, lambda: page.evaluate("window.speechSynthesis.speaking"))
    # Its own echo goes on being ignored; words it never said are the user.
    hear(page, "two reviews and", interim=True)
    assert page.evaluate("window.speechSynthesis.speaking")
    hear(page, "two reviews and what about issue 30", interim=True)
    eventually(page, lambda: not page.evaluate("window.speechSynthesis.speaking"))
    # Sent once the user pauses, without the echo heard in front of it.
    hear(page, "two reviews and what about issue 30")
    expect(page.locator("#voice-status")).to_have_text("It crashes on start.")
    assert assistant.last()["text"] == "what about issue 30"


def test_a_typed_request_never_turns_the_microphone_on_to_read(
    page: Page, dashboard_site, assistant
):
    url, _ = dashboard_site
    assistant.reply("what needs me", "Nothing.")
    page.add_init_script(FAKE_SPEECH)
    page.add_init_script(HELD_SPEECH)
    page.add_init_script('localStorage.setItem("babysit-pr:voice:v1", \'{"speak": true}\')')
    page.goto(url)
    expect(page.locator("#updated")).to_contain_text("Refreshed")
    page.locator("#voice-toggle").click()
    page.locator("#voice-mic").click()
    expect(page.locator("#voice-mic")).to_have_attribute("aria-pressed", "false")
    command(page, "what needs me")
    eventually(page, lambda: page.evaluate("window.speechSynthesis.speaking"))
    expect(page.locator("#voice-mic")).to_have_attribute("aria-pressed", "false")


def test_a_pause_mid_sentence_waits_for_the_rest_and_a_tap_sends_at_once(
    page: Page, dashboard_site, assistant
):
    url, home = dashboard_site
    assistant.reply("tell the agent to rebase on main", "I'll tell it.")
    assistant.reply("what needs me", "Nothing.")
    open_voice(page, url)
    bar = page.locator("#voice-bar")
    # The recognizer ends a result at a breath; a sentence left hanging waits for the rest.
    hear(page, "tell the agent to", settle=False)
    expect(bar).to_have_class(SETTLING)
    page.wait_for_timeout(1900)
    assert assistant.requests == []
    hear(page, "rebase on main")
    assert [request["text"] for request in assistant.requests] == [
        "tell the agent to rebase on main"
    ]
    # Tapping the microphone sends what was said without waiting out the pause.
    hear(page, "what needs me", settle=False)
    mic = page.locator("#voice-mic")
    expect(mic).to_have_attribute("aria-label", "Send now")
    mic.click()
    answered(page)
    assert assistant.last()["text"] == "what needs me"
    expect(page.locator("#voice-status")).to_have_text("Nothing.")

    # Both exchanges are in the transcript, the browser's part under one conversation.
    def heard():
        return [e for e in voice_assistant.transcripts(home) if e["event"] == "heard"]

    eventually(page, lambda: len(heard()) == 2)
    assert [(e["text"], e["how"]) for e in heard()] == [
        ("tell the agent to rebase on main", "pause"),
        ("what needs me", "tap"),
    ]
    assert heard()[0]["waited_ms"] == 1600
    events = voice_assistant.transcripts(home)
    assert {"heard", "reply", "outcomes"} <= {e["event"] for e in events}
    assert len({e["conversation"] for e in events}) == 1
    assert assistant.requests[0]["conversation"] == events[0]["conversation"]


def test_the_time_to_confirm_starts_once_the_question_has_been_read(
    page: Page, dashboard_site, viewer_routes, assistant
):
    url, _ = dashboard_site
    text = (
        "Rebase on main, then rerun the tests, and if the upload test still fails look at "
        "the fixture it reads before changing anything else."
    )
    assistant.reply(
        "tell it to rebase",
        "I'll message the agent.",
        {"type": "message_agent", "workspace": "w1", "name": "x", "text": text},
    )
    page.add_init_script(FAKE_SPEECH)
    page.add_init_script(HELD_SPEECH)
    page.add_init_script('localStorage.setItem("babysit-pr:voice:v1", \'{"speak": true}\')')
    page.clock.install()
    page.goto(url)
    expect(page.locator("#updated")).to_contain_text("Refreshed")
    page.locator("#voice-toggle").click()
    command(page, "tell it to rebase")
    status = page.locator("#voice-status")
    # Shown whole, but read out by its start.
    expect(status).to_contain_text(f"Say “confirm” to send “{text}” to workspace w1")
    eventually(page, lambda: page.evaluate("window.spoken").__len__() == 1)
    assert page.evaluate("window.spoken") == [
        "I'll message the agent. Say confirm to send your 24-word message, starting "
        "“Rebase on main, then rerun the tests, and if the upload test…”, to "
        "workspace w1, or cancel."
    ]
    # A slow reading does not use up the time to answer it.
    page.clock.fast_forward(15000)
    page.evaluate("window.speechSynthesis.cancel()")
    page.clock.fast_forward(40000)
    command(page, "confirm")
    expect(page.locator("#ws-message-status")).to_contain_text("Sent to the agent")
    assert viewer_routes["sent"][0][1]["text"] == text


def test_replies_are_spoken_unless_turned_off_and_the_choice_is_kept(
    page: Page, dashboard_site, assistant
):
    url, _ = dashboard_site
    assistant.reply("anything new", "Nothing new since this morning.")
    open_voice(page, url)
    command(page, "help")
    expect(page.locator("#voice-help")).to_be_visible()
    expect(page.locator("#voice-speak")).not_to_be_checked()
    command(page, "anything new")
    expect(page.locator("#voice-status")).to_have_text("Nothing new since this morning.")
    assert page.evaluate("window.spoken") == []
    page.locator("#voice-speak").check()
    command(page, "anything new")
    expect(page.locator("#voice-status")).to_have_text("Nothing new since this morning.")
    assert page.evaluate("window.spoken") == ["Nothing new since this morning."]
    page.reload()
    page.locator("#voice-toggle").click()
    page.locator("#voice-help-toggle").click()
    expect(page.locator("#voice-speak")).to_be_checked()


def test_new_task_opens_with_the_spoken_task(page: Page, dashboard_site, viewer_routes, assistant):
    url, _ = dashboard_site
    page.route(
        "**/api/workspace-repos",
        lambda route: route.fulfill(
            json={
                "repos": [
                    {"repo": "up/alpha", "clone": "/fixture/alpha", "remote": "origin", "active": 1}
                ],
                "idle": 0,
            }
        ),
    )
    assistant.reply(
        "start a task to fix the flaky upload test",
        "The New task form is filled in; pick the repository and start it.",
        {"type": "command", "text": "new task fix the flaky upload test"},
    )
    open_voice(page, url)
    hear(page, "start a task to fix the flaky upload test")
    dialog = page.locator("#workspace-dialog")
    expect(dialog.get_by_label("Task", exact=True)).to_have_value("fix the flaky upload test")


def test_alt_v_listens_inside_a_dialog(page: Page, dashboard_site, assistant):
    url, _ = dashboard_site
    page.add_init_script(FAKE_SPEECH)
    page.goto(url)
    page.locator("#usage-toggle").click()
    dialog = page.locator("#usage-dialog")
    expect(dialog).to_be_visible()
    page.keyboard.press("Alt+KeyV")
    expect(dialog.locator("#voice-bar")).to_be_visible()
    hear(page, "close")
    expect(dialog).to_be_hidden()
    assert assistant.last()["context"]["dialog"]


def test_without_speech_recognition_the_bar_takes_typed_requests(
    page: Page, dashboard_site, assistant
):
    url, _ = dashboard_site
    page.add_init_script(NO_SPEECH)
    page.goto(url)
    page.locator("#voice-toggle").click()
    expect(page.locator("#voice-input")).to_be_focused()
    expect(page.locator("#voice-status")).to_contain_text("This browser has no speech recognition")
    command(page, "issues")
    expect(page.locator("#issues-panel")).to_be_visible()
    page.locator("#voice-input").press("Escape")
    expect(page.locator("#voice-bar")).to_be_hidden()


def test_the_assistant_cannot_get_round_confirmation(
    page: Page, dashboard_site, viewer_routes, assistant
):
    url, _ = dashboard_site
    agent = viewer_routes["agents"][1]
    agent.update(
        status="blocked",
        session="live-session",
        interaction={
            "screen": "",
            "question": None,
            "idle": False,
            "choices": {
                "id": "dialog:abc",
                "text": "Allow this command?",
                "options": [
                    {"key": "1", "label": "Yes"},
                    {"key": "2", "label": "Yes, and don't ask again for this command"},
                    {"key": "3", "label": "No"},
                ],
            },
        },
    )
    viewer_routes["agents"] = [agent]
    chosen = []
    page.route(
        "**/api/workspace-choose",
        lambda route: (
            chosen.append(route.request.post_data_json),
            route.fulfill(json={"chosen": True, "pane": "w1:p2"}),
        ),
    )
    docker = []
    page.route(
        "**/api/workspace-docker",
        lambda route: (docker.append(1), route.fulfill(json={"enabled": True})),
    )
    page.clock.install()
    open_voice(page, url, "/#prs")
    page.locator("#pr-list tr").first.get_by_role("button", name="Transcript", exact=True).click()
    interaction = page.locator("#ws-agent-interaction")
    expect(interaction.get_by_role("button", name="No", exact=True)).to_be_visible()
    status = page.locator("#voice-status")
    # "Sort by" presses only sort controls, never a long-named answer that resembles one.
    command(page, "sort by yes and dont ask again for this command")
    expect(status).to_contain_text("That did not work")
    assert chosen == []
    # A checkbox, which may grant Docker access or quit an agent, waits like any change.
    quit = page.locator("#ws-message-quit")
    expect(quit).to_be_visible()
    command(page, "check quit the agent when it is done")
    expect(status).to_have_text(
        "Say “confirm” to press Quit the agent when it is done, or “cancel”."
    )
    command(page, "cancel")
    expect(quit).not_to_be_checked()
    assert docker == []
    # Cancel wins on any of the recognizer's guesses; confirm needs its best one.
    command(page, "option 1")
    expect(status).to_contain_text("Say “confirm” to press Yes")
    page.locator("#voice-mic").click()
    hear(page, "don't", "do it")
    expect(status).to_have_text("Cancelled.")
    assert chosen == []
    # A confirmation that waited too long is not sent on to the assistant.
    command(page, "option 1")
    page.clock.fast_forward(46000)
    asked = len(assistant.requests)
    command(page, "confirm")
    expect(status).to_have_text("That waited too long, so nothing was done. Ask again.")
    assert chosen == [] and len(assistant.requests) == asked


def test_a_newer_request_or_start_over_cancels_the_answer_in_progress(
    page: Page, dashboard_site, assistant
):
    url, _ = dashboard_site
    open_voice(page, url)
    assistant.held = True
    command(page, "first question", wait=False)
    expect(page.locator("#voice-bar")).to_have_class(THINKING)
    command(page, "second question", wait=False)
    eventually(page, lambda: assistant.cancelled == [f"{1:032x}"])
    command(page, "start over")
    expect(page.locator("#voice-status")).to_have_text("Starting over.")
    expect(page.locator("#voice-bar")).not_to_have_class(THINKING)
    eventually(page, lambda: assistant.cancelled == [f"{1:032x}", f"{2:032x}"])


def test_spoken_numbers_are_read_as_numbers(page: Page, dashboard_site):
    url, _ = dashboard_site
    page.goto(url)
    cases = {
        "8": 8,
        "#12": 12,
        "number five": 5,
        "twenty three": 23,
        "three hundred and twenty one": 321,
        "one hundred five": 105,
        "two three": None,
        "twelve four": None,
        "main": None,
        # Recognizers group long numbers with commas.
        "19,876": 19876,
    }
    for text, expected in cases.items():
        assert page.evaluate("text => window.dashboardVoice.numberFrom(text)", text) == expected, (
            text
        )


def test_the_bar_fits_a_phone_and_keeps_the_new_task_button_clear(page: Page, dashboard_site):
    url, _ = dashboard_site
    page.set_viewport_size({"width": 390, "height": 844})
    page.add_init_script(FAKE_SPEECH)
    page.goto(url)
    page.locator("#voice-toggle").click()
    # The New task button moves up once the bar's height is known.
    eventually(
        page,
        lambda: page.evaluate("document.body.style.getPropertyValue('--voice-bar-height')") != "",
    )
    bar = page.locator("#voice-bar").bounding_box()
    assert bar["x"] >= 16 and bar["x"] + bar["width"] <= 390 - 16 + 0.5
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    button = page.get_by_role("button", name="New task").bounding_box()
    assert button["y"] + button["height"] <= bar["y"]
    size = page.locator("#voice-input").evaluate("el => getComputedStyle(el).fontSize")
    assert size == "16px"
    page.screenshot(path="reports/voice-bar-mobile.png")
