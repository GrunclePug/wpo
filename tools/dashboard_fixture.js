// Synthetic browser transport: never contacts a project, provider, or action endpoint.
window.fixture.requests = [];
window.EventSource = class extends EventTarget {
  constructor() {
    super();
    setTimeout(() => {
      this.dispatchEvent(new Event("open"));
      this.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify(fixture.snapshot) }));
      this.dispatchEvent(new MessageEvent("events", { data: JSON.stringify({ events: fixture.events, reset: true }) }));
    });
  }
  close() {}
};
window.fetch = async (url, options) => {
  if (url === "/api/state") return Response.json({ snapshot: fixture.snapshot, events: fixture.events });
  if (url.startsWith("/api/wp/")) return fixture.failDetail ? new Response("Synthetic detail error", { status: 500 }) : Response.json(fixture.detail);
  if (url.startsWith("/api/action/")) {
    fixture.requests.push({ url, body: JSON.parse(options.body) });
    return Response.json(fixture.failAction ? { ok: false, error: `Synthetic action error: ${fixture.snapshot.wps[0].title}` } : { ok: true, message: "Synthetic action recorded" }, { status: fixture.failAction ? 400 : 200 });
  }
  throw new Error(`Unexpected fixture request: ${url}`);
};

window.runDashboardChecks = async (expectedWidth, expectedHeight) => {
  const passed = [];
  const check = (condition, name) => { if (!condition) throw new Error(name); passed.push(name); };
  // T3 applies a viewport change asynchronously; measure the requested layout before checking it.
  if (expectedWidth) {
    for (let attempt = 0; attempt < 40 && (innerWidth !== expectedWidth || innerHeight !== expectedHeight); attempt++) {
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    check(innerWidth === expectedWidth && innerHeight === expectedHeight, "requested viewport applied");
  }
  const snapshot = structuredClone(fixture.snapshot);
  const input = (element, value) => { element.value = value; element.dispatchEvent(new Event("input", { bubbles: true })); };
  const settle = () => new Promise((resolve) => setTimeout(resolve, 30));
  const noOverflow = (element) => element.scrollWidth <= element.clientWidth + 1;
  check(noOverflow(document.documentElement), "page has no horizontal overflow");
  for (const table of document.querySelectorAll(".grid")) {
    if (innerWidth <= 1100) check(noOverflow(table), "narrow table reflows");
  }
  check(document.querySelectorAll("#wps-body .row").length >= 4, "grouped rows render");
  check(document.querySelector('#wps-body .row').dataset.rowWp === "WP-0001", "active work sorts first");
  const group = document.querySelector('[data-toggle-group="inflight"]');
  group.focus();
  group.click();
  check(document.activeElement.dataset.focusKey === "toggle-inflight" && document.activeElement.getAttribute("aria-expanded") === "false", "group toggle retains focus");
  document.activeElement.click();
  const filter = document.querySelector("#wp-filter");
  filter.focus();
  input(filter, "WP-0003");
  check(document.querySelectorAll("#wps-body .row").length === 1, "filter reveals dependency group");
  receiveSnapshot(snapshot);
  check(document.activeElement === filter && filter.value === "WP-0003", "filter focus survives snapshot");
  input(filter, "absent");
  check(document.querySelector("#wps-body").textContent.includes("No open work package matches"), "empty filter state");
  input(filter, "");
  document.querySelector('a[href="#needs-you"]').click();
  check(document.querySelector("#needs-disclosure").open, "needs-you link reveals forms");
  const form = document.querySelector('[data-key="unblock:WP-0004"]');
  const textarea = form.querySelector("textarea");
  textarea.focus();
  input(textarea, "Keep this synthetic draft");
  textarea.setSelectionRange(5, 9);
  const select = form.querySelector("select");
  select.value = "review";
  select.dispatchEvent(new Event("change", { bubbles: true }));
  const changed = structuredClone(snapshot);
  changed.wps[3].latest_progress += " refreshed";
  receiveSnapshot(changed);
  const draft = document.querySelector('[data-key="unblock:WP-0004"] textarea');
  check(draft.value === "Keep this synthetic draft" && document.activeElement === draft && draft.selectionStart === 5 && draft.selectionEnd === 9, "draft and text selection survive changed snapshot");
  check(document.querySelector('[data-key="unblock:WP-0004"] select').value === "review", "status choice survives snapshot");
  check(document.querySelector("#needs-disclosure").open, "disclosure survives snapshot");
  document.querySelector("#usage-disclosure").open = true;
  document.querySelector("#native-disclosure").open = true;
  document.querySelector('[popovertarget="cooldowns"]').click();
  receiveSnapshot(snapshot);
  check(Boolean(document.querySelector("#cooldowns:popover-open")), "cooldowns stay open through snapshot");
  document.querySelector("#cooldowns").hidePopover();
  check(noOverflow(document.documentElement), "expanded summaries have no page overflow");
  document.querySelector("#usage-disclosure").open = false;
  document.querySelector("#native-disclosure").open = false;
  fixture.failAction = true;
  const submission = submitAction(document.querySelector('[data-key="unblock:WP-0004"]'));
  check(document.querySelector('[data-key="unblock:WP-0004"] button').disabled, "pending form disables submission");
  await submission;
  check(fixture.requests.at(-1).body.status === "review", "unblock sends selected status");
  check(document.querySelector(".action-outcome.is-error")?.textContent.includes("Synthetic action error"), "action error remains readable");
  fixture.failAction = false;
  const approval = document.querySelector('[data-key="approval:synthetic-approval"]');
  input(approval.querySelector("textarea"), "Synthetic reason");
  await submitAction(approval, approval.querySelector('[value="deny"]'));
  check(fixture.requests.at(-1).url.endsWith("/deny") && fixture.requests.at(-1).body.reason === "Synthetic reason", "deny action semantics");
  const request = document.querySelector('#request-box form');
  input(request.querySelector("textarea"), "Synthetic request");
  await submitAction(request);
  check(fixture.requests.at(-1).body.text === "Synthetic request", "request action semantics");
  document.querySelector('[data-feed-filter="attention"]').click();
  check(document.querySelectorAll("#events .event").length === 1, "event filter");
  document.querySelector('[data-feed-filter="all"]').click();
  const eventMessage = document.querySelector(".event-message");
  const wasExpanded = eventMessage.getAttribute("aria-expanded");
  eventMessage.focus();
  eventMessage.click();
  receiveEvents([], false);
  check(document.activeElement.dataset.focusKey === eventMessage.dataset.focusKey && document.activeElement.getAttribute("aria-expanded") !== wasExpanded, "event expansion and focus survive refresh");
  openDetail("WP-0004");
  check(!document.querySelector("#detail").hidden, "detail loading shell");
  await settle();
  check(document.querySelector("#detail-title").textContent.includes("Synthetic blocked"), "detail loads");
  check(noOverflow(document.querySelector("#detail-body")), "detail has no horizontal overflow");
  check(noOverflow(document.querySelector("#detail-actions")), "long action error has no horizontal overflow");
  check(document.querySelector("#detail-body").clientHeight > 60 && document.querySelector("#detail-actions").clientHeight > 60, "short viewport keeps details and actions scrollable");
  const message = document.querySelector('#detail-actions [data-action="message"] textarea');
  message.focus();
  input(message, "Synthetic message");
  document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  check(!document.querySelector("#detail").hidden, "Escape preserves focused draft");
  document.querySelector("#detail-close").focus();
  document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  check(document.querySelector("#detail").hidden && document.activeElement.dataset.openWp === "WP-0004", "Escape closes and restores focus");
  fixture.failDetail = true;
  openDetail("WP-0004");
  await settle();
  check(document.querySelector("#detail-body").textContent.includes("Synthetic detail error"), "detail error state");
  fixture.failDetail = false;
  closeDetail();
  receiveSnapshot({ ...snapshot, wps: [], lanes: [], approvals: [], native_backend: null });
  check(document.querySelector("#wps-body").textContent.includes("No open work packages"), "empty board state");
  check(document.querySelector("#native-disclosure").hidden && document.querySelector("#needs-you").hidden, "empty attention sections hidden");
  receiveSnapshot(snapshot);
  document.querySelector("#needs-disclosure").open = false;
  document.querySelectorAll(".toast").forEach((element) => element.remove());
  document.activeElement.blur();
  scrollTo(0, 0);
  return { width: innerWidth, height: innerHeight, passed };
};
