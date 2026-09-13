const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
class Node {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.value = "";
    this.textContent = "";
    this.attributes = {};
  }
  append(...nodes) {
    this.children.push(...nodes);
  }
  replaceChildren(...nodes) {
    this.children = nodes;
  }
  setAttribute(key, value) {
    this.attributes[key] = value;
  }
  scrollIntoView() {}
}
const nodes = new Map();
const get = (id) => {
  if (!nodes.has(id)) nodes.set(id, new Node("div"));
  return nodes.get(id);
};
get("filter").value = "active";
const job = {
  id: "one",
  status: "watching",
  repo: "test/repo",
  kind: "pr",
  branch: "feature",
  summary: "Waiting",
  attempts: 0,
  max_repairs: 5,
  pending_reviews: 1,
  feedback_approved: 0,
  feedback_token: "reviewed-batch",
  check_details: [],
  failed_jobs: [],
  feedback: [
    {
      kind: "issue_comment",
      id: "1",
      author: "reviewer",
      body: "<img src=x onerror=alert(1)>",
      url: "https://github.com/test/repo/issues/1",
    },
  ],
};
const fixture = { jobs: [job], daemon: { health: "healthy", max_workers: 2 }, home: "/fixture" };
const requests = [];
const context = vm.createContext({
  URL,
  URLSearchParams,
  location: { search: "" },
  console,
  fixture,
  window: {
    location: { hash: "" },
    addEventListener() {},
    matchMedia: () => ({ matches: false }),
  },
  document: {
    querySelectorAll: () => [],
    getElementById: get,
    createElement: (tag) => new Node(tag),
    addEventListener() {},
  },
  setInterval() {},
  fetch: async (url, options) => {
    requests.push({ url, options });
    return {
      ok: true,
      json: async () =>
        url === "/api/status" ? fixture : { job: { ...job, feedback_approved: 1 } },
    };
  },
});
vm.runInContext(
  fs.readFileSync(new URL("../assets/dashboard/app.js", `file://${__filename}`), "utf8"),
  context,
);
const all = (node) => [node, ...node.children.flatMap(all)];
(async () => {
  vm.runInContext('data=fixture;selected="one";render()', context);
  assert(!requests.some((r) => r.options?.method === "POST"));
  const detail = all(get("detail"));
  assert(detail.some((n) => n.textContent === job.feedback[0].body));
  const button = detail.find((n) => n.textContent === "Handle feedback");
  assert(button && !button.disabled);
  await button.onclick();
  const request = requests.find((r) => r.options?.method === "POST");
  assert.equal(request.url, "/api/feedback");
  assert.deepEqual(JSON.parse(request.options.body), { id: "one", token: "reviewed-batch" });
  fixture.jobs = [{ ...job, status: "closed", cleanup_ready: true, pr_outcome: "merged" }];
  vm.runInContext("data=fixture;render()", context);
  assert.equal(get("attention").textContent, 1);
  get("show-attention").onclick();
  assert.equal(get("filter").value, "attention");
  assert.equal(get("count").textContent, 1);
  vm.runInContext('selected="one";render()', context);
  assert(!all(get("detail")).some((n) => n.textContent === "Handle feedback"));
  assert(all(get("detail")).some((n) => n.textContent === "PR merged · ready for cleanup"));
  console.log(
    "Dashboard feedback click, exact batch token, plain-text preview, and cleanup attention checks passed",
  );
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
