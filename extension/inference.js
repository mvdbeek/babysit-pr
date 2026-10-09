import { githubTarget } from "./shared.js";

export function githubRepo(value) {
  try {
    const url = new URL(value);
    const match = url.pathname.match(/^\/([\w.-]+\/[\w.-]+)(?:\/|$)/);
    return url.origin === "https://github.com" && match ? match[1] : null;
  } catch {
    return null;
  }
}

export function pageKey(source) {
  const repo = githubRepo(source.url);
  if (repo) return `repo:${repo.toLowerCase()}`;
  try {
    return new URL(source.url).origin;
  } catch {
    return "";
  }
}

export function infer(source, context, preferences = {}, task = "") {
  const github = githubTarget(source.url);
  const target =
    github &&
    context.targets.find(
      (t) => githubTarget(t.url)?.url.toLowerCase() === github.url.toLowerCase(),
    );
  const explicit = context.repos.filter((r) =>
    new RegExp(
      `(?:^|[^\\w/.-])${r.repo.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?=$|[^\\w/.-])`,
      "i",
    ).test(task),
  );
  const quoted = context.repos.filter((r) =>
    new RegExp(
      `(?:^|[^\\w/.-])${r.repo.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?=$|[^\\w/.-])`,
      "i",
    ).test(`${source.selection || ""}\n${source.title || ""}`),
  );
  const linked = new Set(
    (source.links || "")
      .split("\n")
      .map(githubRepo)
      .filter(Boolean)
      .map((r) => r.toLowerCase()),
  );
  const linkedRepos = [
    ...new Set(
      context.repos
        .filter((r) => linked.has(r.repo.toLowerCase()))
        .map((r) => r.repo.toLowerCase()),
    ),
  ];
  const remembered = preferences.sites?.[pageKey(source)] || preferences.last;
  const requestedRepo =
    githubRepo(source.url) ||
    (new Set(explicit.map((r) => r.repo)).size === 1 ? explicit[0]?.repo : null) ||
    (new Set(quoted.map((r) => r.repo)).size === 1 ? quoted[0]?.repo : null) ||
    (linkedRepos.length === 1 ? linkedRepos[0] : null);
  let candidates = requestedRepo
    ? context.repos.filter((r) => r.repo.toLowerCase() === requestedRepo.toLowerCase())
    : context.repos;
  const rememberedClone = candidates.find(
    (r) => r.repo === remembered?.repo && r.clone === remembered?.clone,
  );
  const preferred =
    target?.preferred_clone && candidates.find((r) => r.clone === target.preferred_clone);
  let repository = preferred || rememberedClone;
  if (!repository && (requestedRepo || new Set(candidates.map((r) => r.repo)).size === 1))
    repository = candidates[0];
  const agents = context.agents || [];
  const matching = agents
    .filter((a) => {
      if (github)
        return (a.targets || []).some(
          (url) => githubTarget(url)?.url.toLowerCase() === github.url.toLowerCase(),
        );
      try {
        const page = new URL(source.url),
          workspace = new URL(a.url);
        return (
          page.origin === workspace.origin &&
          page.pathname.replace(/\/$/, "") === workspace.pathname.replace(/\/$/, "")
        );
      } catch {
        return false;
      }
    })
    .filter((a) => a.session);
  return {
    target: target || null,
    repository: repository || null,
    requestedRepo,
    matching,
    suggestedAgents: agents.filter(
      (a) => repository && (a.repos || []).includes(repository.repo.toLowerCase()),
    ),
    reason: requestedRepo
      ? "From this page or task"
      : rememberedClone
        ? "Your previous choice"
        : repository
          ? "Only repository available"
          : "Choose a repository",
  };
}

export function suggestedTask(source, target) {
  const url = source.url || "";
  if (source.selection?.trim())
    return "Investigate the selected text and address the actionable problems it describes. Use the page context to understand it.";
  if (target?.kind === "issue" || githubTarget(url)?.url.includes("/issues/"))
    return "Investigate this issue and implement a fix with appropriate tests.";
  if (
    ["FAILURE", "ERROR"].includes(target?.ci) ||
    /\/actions\/runs\//.test(url) ||
    /\/pull\/\d+\/checks/.test(url)
  )
    return "Diagnose and fix the failing CI checks. Run the relevant tests, commit and push the fix.";
  if (target?.review_decision === "CHANGES_REQUESTED" || target?.unresolved_threads > 0)
    return "Review the outstanding feedback on this PR and address the valid findings. Run the relevant tests, commit and push. Do not reply to or resolve review threads.";
  if (githubTarget(url))
    return "Review this PR for actionable bugs and regressions. Report findings without posting a GitHub review.";
  if (/\/blob\//.test(url))
    return "Inspect this file and explain the relevant behavior and any actionable problems.";
  return "";
}

export function branchName(task, title, suffix) {
  const slug = (task || title || "browser-task")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48)
    .replace(/-+$/, "");
  return `${slug || "browser-task"}-${suffix}`;
}
