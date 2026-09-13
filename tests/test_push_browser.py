"""Device permission and badge behavior with fake push endpoints on a temporary server."""

import copy

import pytest
from playwright.sync_api import expect
from test_dashboard_browser import dashboard_site as dashboard_site
from test_notifications_browser import count
from test_notifications_browser import inbox as inbox

pytestmark = pytest.mark.browser
URL = "https://github.com/test/alpha/pull/8"


@pytest.fixture
def push_browser(page, inbox):
    url, packets = inbox
    state = {
        "login": "fixture",
        "token": "device-token",
        "active": True,
        "error": None,
        "entries": {
            URL: {
                "title": "Background update",
                "at": 2000,
                "seenAt": 0,
                "notes": {"prs": {"at": 2000, "text": "CI updated"}},
            }
        },
        "observed": {"prs": {URL: packets["prs"]["synced_at"] * 1000}},
    }
    requests = []
    failure = [False]

    def route(request):
        action = request.request.url.rsplit("push-", 1)[1]
        if action == "config":
            request.fulfill(json={"available": True, "publicKey": "BA" + "A" * 85})
            return
        data = request.request.post_data_json
        requests.append((action, data))
        if failure[0]:
            request.fulfill(status=503, json={"error": "Offline"})
            return
        if action == "seen":
            for key, at in data["seen"].items():
                state["entries"][key]["seenAt"] = max(state["entries"][key]["seenAt"], at)
        request.fulfill(json=copy.deepcopy(state))

    page.route("**/api/push-*", route)
    page.add_init_script("""(() => {
      window.pushTest = {permissions: 0, badges: [], messages: [], subscriptions: 0, permission: 'granted'};
      Notification.requestPermission = async () => { pushTest.permissions++; return pushTest.permission; };
      Object.defineProperty(navigator, 'setAppBadge', {value: async count => pushTest.badges.push(count)});
      Object.defineProperty(navigator, 'clearAppBadge', {value: async () => pushTest.badges.push(0)});
      const subscription = {toJSON: () => ({endpoint: 'https://web.push.apple.com/fake', keys: {auth:'fake', p256dh:'fake'}}), unsubscribe: async () => true};
      const registration = {active: {postMessage: data => pushTest.messages.push(data)},
        pushManager: {getSubscription: async () => null, subscribe: async () => { pushTest.subscriptions++; return subscription; }}};
      Object.defineProperty(navigator, 'serviceWorker', {value: {register: async () => registration,
        ready: Promise.resolve(registration), addEventListener: () => {}}});
    })();""")
    return url, state, requests, failure


def enable(page, url):
    page.goto(url)
    count(page, 0)
    page.locator("#notifications-toggle").click()
    page.get_by_role("button", name="Enable notifications", exact=True).click()
    expect(page.get_by_role("button", name="Disable notifications", exact=True)).to_be_visible()
    count(page, 1)


def test_opt_in_badge_and_mark_all_seen_persist_across_reload(page, push_browser):
    url, state, requests, _ = push_browser
    page.goto(url)
    count(page, 0)
    assert page.evaluate("pushTest.permissions") == 0
    assert not requests
    page.locator("#notifications-toggle").click()
    page.get_by_role("button", name="Enable notifications", exact=True).click()
    count(page, 1)
    expect(page.locator("#notifications-list")).to_contain_text("Background update")
    assert page.evaluate("pushTest.badges.at(-1)") == 1
    assert page.evaluate("pushTest.permissions") == 1
    with page.expect_response("**/api/push-seen"):
        page.get_by_role("button", name="Mark all seen", exact=True).click()
    count(page, 0)
    expect(page.locator("#notifications-push-status")).to_contain_text("enabled on this device")
    assert state["entries"][URL]["seenAt"] == 2000
    assert page.evaluate("pushTest.badges.at(-1)") == 0
    page.reload()
    count(page, 0)
    page.locator("#notifications-toggle").click()
    page.get_by_role("button", name="Disable notifications", exact=True).click()
    expect(page.get_by_role("button", name="Enable notifications", exact=True)).to_be_visible()
    assert any(action == "unsubscribe" for action, _ in requests)
    assert page.evaluate("pushTest.badges.at(-1)") == 0


def test_denied_permission_does_not_subscribe(page, push_browser):
    url, _, requests, _ = push_browser
    page.goto(url)
    count(page, 0)
    page.evaluate("pushTest.permission = 'denied'")
    page.locator("#notifications-toggle").click()
    page.get_by_role("button", name="Enable notifications", exact=True).click()
    expect(page.locator("#notifications-push-status")).to_contain_text("Allow notifications")
    assert not requests


def test_seen_retries_after_network_recovery_without_losing_new_update(page, push_browser):
    url, state, _, failure = push_browser
    enable(page, url)
    failure[0] = True
    page.get_by_role("button", name="Mark all seen", exact=True).click()
    count(page, 0)
    expect(page.locator("#notifications-push-status")).to_contain_text("Offline")
    state["entries"][URL]["at"] = 3000
    failure[0] = False
    page.evaluate("window.dispatchEvent(new Event('online'))")
    count(page, 1)
    assert state["entries"][URL]["seenAt"] == 2000


def test_visiting_current_list_clears_badge_but_stale_list_does_not(page, push_browser):
    url, state, _, _ = push_browser
    enable(page, url)
    page.get_by_role("button", name="Close notifications", exact=True).click()
    page.evaluate("dashboardPush.seen(['https://github.com/test/alpha/pull/8'], 'prs', 1)")
    count(page, 1)
    page.get_by_role("tab", name="Pull requests", exact=True).click()
    count(page, 0)
    assert page.evaluate("pushTest.badges.at(-1)") == 0


def test_manifest_and_real_service_worker_background_badge(page, inbox):
    url, _ = inbox
    page.goto(url)
    count(page, 0)
    manifest = page.request.get(f"{url}/manifest.webmanifest").json()
    assert manifest["display"] == "standalone" and manifest["id"] == "/"
    for icon in manifest["icons"]:
        response = page.request.get(url + icon["src"])
        assert response.ok and response.body().startswith(b"\x89PNG")
    page.evaluate("navigator.serviceWorker.ready")
    worker = page.context.service_workers[0]
    worker.evaluate("""() => {
      self.testNotifications = []; self.testBadges = [];
      self.registration.showNotification = async (title, options) => testNotifications.push({title, options});
      Object.defineProperty(navigator, 'setAppBadge', {value: async count => testBadges.push(count)});
      self.testEvent = async (name, properties) => {
        const event = new Event(name); const promises = [];
        Object.assign(event, properties, {waitUntil: promise => promises.push(promise)});
        self.dispatchEvent(event); await Promise.all(promises);
      };
    }""")
    worker.evaluate("testEvent('push', {data: {json: () => ({count: 4, revision: 100})}})")
    assert worker.evaluate("testBadges.at(-1)") == 4
    assert worker.evaluate("testNotifications.at(-1).options.body").startswith("4 unseen items")
    # An already queued older push must not resurrect a badge cleared in the app.
    worker.evaluate(
        "testEvent('message', {data: {type: 'inbox', count: 0, token: null, revision: 200}})"
    )
    worker.evaluate("testEvent('push', {data: {json: () => ({count: 4, revision: 100})}})")
    assert worker.evaluate("testBadges.at(-1)") == 0
    assert worker.evaluate("testNotifications.length") == 2
    page.goto(url + "/?updates=1")
    expect(page.get_by_role("dialog", name="Recent updates")).to_be_visible()
