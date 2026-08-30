import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";


class FakeClassList {
  add() {}
  remove() {}
  toggle() {}
}


class FakeElement {
  constructor(tagName = "div") {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.classList = new FakeClassList();
    this.textContent = "";
    this.value = "";
    this.hidden = false;
    this.disabled = false;
    this.listeners = new Map();
  }

  get firstChild() {
    return this.children[0] || null;
  }

  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }

  replaceChildren(...children) {
    this.children.forEach((child) => {
      child.parentNode = null;
    });
    this.children = [];
    children.forEach((child) => this.appendChild(child));
  }

  addEventListener(name, callback) {
    this.listeners.set(name, callback);
  }

  setAttribute(name, value) {
    this[name] = String(value);
  }

  removeAttribute(name) {
    delete this[name];
  }

  querySelector() {
    return null;
  }

  querySelectorAll() {
    return [];
  }

  click() {}
  remove() {}
}


function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}


function jsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    body: null,
    headers: {
      get(name) {
        return String(name).toLowerCase() === "content-type"
          ? "application/json"
          : null;
      },
    },
    async json() {
      return payload;
    },
    async text() {
      return JSON.stringify(payload);
    },
  };
}


function job(jobId, originalName = `${jobId}.pdf`) {
  return {
    job_id: jobId,
    original_name: originalName,
    state: "succeeded",
    artifact_state: "available",
    progress_percent: 100,
  };
}


function createHarness() {
  const elements = new Map();
  const document = {
    body: new FakeElement("body"),
    createElement(tagName) {
      return new FakeElement(tagName);
    },
    getElementById(id) {
      if (!elements.has(id)) {
        elements.set(id, new FakeElement());
      }
      return elements.get(id);
    },
    querySelectorAll() {
      return [];
    },
  };
  let fetchImplementation = async () => {
    throw new Error("unexpected fetch");
  };
  const hook = {};
  const context = vm.createContext({
    __DOCLING_UI_TEST__: hook,
    document,
    window: { confirm: () => true },
    fetch: (...args) => fetchImplementation(...args),
    console,
    URL,
    URLSearchParams,
    setInterval,
    clearInterval,
  });
  const sourcePath = fileURLToPath(
    new URL("../docling_service/ui/main.js", import.meta.url),
  );
  vm.runInContext(fs.readFileSync(sourcePath, "utf8"), context, {
    filename: sourcePath,
  });
  return {
    hook,
    element(id) {
      return document.getElementById(id);
    },
    setFetch(implementation) {
      fetchImplementation = implementation;
    },
  };
}


function seedSecondPage(state) {
  state.config = { revision: 1, editable: {}, readonly: {} };
  state.jobs = [job("page-2")];
  state.jobsQuery = "";
  state.jobsPageIndex = 1;
  state.jobsCursorHistory = [null, "cursor-1", "cursor-2"];
  state.jobsNextCursor = "cursor-2";
}


test("token changes clear jobs and reject a delayed navigation response", async () => {
  const harness = createHarness();
  const { UI_STATE, navigateJobsPage, setToken } = harness.hook;
  const delayed = deferred();

  setToken("token-a");
  seedSecondPage(UI_STATE);
  harness.setFetch(() => delayed.promise);
  const navigation = navigateJobsPage(1);
  assert.equal(UI_STATE.jobsLoadingPage, true);

  setToken("token-b");
  assert.deepEqual(Array.from(UI_STATE.jobs), []);
  assert.equal(UI_STATE.jobsLoadingPage, false);
  delayed.resolve(jsonResponse({ items: [job("private-a")], next_cursor: null }));
  await navigation;

  assert.deepEqual(Array.from(UI_STATE.jobs), []);
  assert.equal(UI_STATE.token, "token-b");
});


test("old refresh errors cannot reset a page reached by newer navigation", async () => {
  const harness = createHarness();
  const { UI_STATE, navigateJobsPage, refreshDashboardData } = harness.hook;
  const refreshJobs = deferred();
  const navigationJobs = deferred();
  seedSecondPage(UI_STATE);
  harness.setFetch((url) => {
    const value = String(url);
    if (value === "/v1/capabilities") {
      return Promise.resolve(jsonResponse({ ok: true }));
    }
    if (value === "/v1/system/storage") {
      return Promise.resolve(jsonResponse({ usage: {}, limits: {} }));
    }
    if (value.includes("cursor=cursor-1")) {
      return refreshJobs.promise;
    }
    if (value.includes("cursor=cursor-2")) {
      return navigationJobs.promise;
    }
    throw new Error(`unexpected fetch: ${value}`);
  });

  const refresh = refreshDashboardData();
  const navigation = navigateJobsPage(1);
  navigationJobs.resolve(
    jsonResponse({ items: [job("page-3")], next_cursor: "cursor-3" }),
  );
  assert.equal(await navigation, true);
  refreshJobs.resolve(jsonResponse({ detail: "stale cursor" }, 400));
  assert.equal(await refresh, false);

  assert.equal(UI_STATE.jobsPageIndex, 2);
  assert.equal(UI_STATE.jobs[0].job_id, "page-3");
  assert.equal(UI_STATE.jobsNextCursor, "cursor-3");
});


test("cursor history loops are quarantined without replacing the current page", async () => {
  const harness = createHarness();
  const { UI_STATE, navigateJobsPage } = harness.hook;
  seedSecondPage(UI_STATE);
  harness.setFetch(() => Promise.resolve(
    jsonResponse({ items: [job("duplicate-page")], next_cursor: "cursor-1" }),
  ));

  assert.equal(await navigateJobsPage(1), false);
  assert.equal(UI_STATE.jobsPageIndex, 1);
  assert.equal(UI_STATE.jobs[0].job_id, "page-2");
  assert.equal(UI_STATE.jobsNextCursor, null);
  assert.match(harness.element("serviceHealth").textContent, /回环/);
});


test("refresh rejects a non-advancing cursor and keeps the visible page", async () => {
  const harness = createHarness();
  const { UI_STATE, refreshDashboardData } = harness.hook;
  seedSecondPage(UI_STATE);
  harness.setFetch((url) => {
    const value = String(url);
    if (value === "/v1/capabilities") {
      return Promise.resolve(jsonResponse({ ok: true }));
    }
    if (value === "/v1/system/storage") {
      return Promise.resolve(jsonResponse({ usage: {}, limits: {} }));
    }
    if (value.includes("cursor=cursor-1")) {
      return Promise.resolve(
        jsonResponse({ items: [job("duplicate-page")], next_cursor: "cursor-1" }),
      );
    }
    throw new Error(`unexpected fetch: ${value}`);
  });

  assert.equal(await refreshDashboardData(), false);
  assert.equal(UI_STATE.jobsPageIndex, 1);
  assert.equal(UI_STATE.jobs[0].job_id, "page-2");
  assert.equal(UI_STATE.jobsNextCursor, null);
});


test("manual refresh cancels navigation and reloads the visible page", async () => {
  const harness = createHarness();
  const { UI_STATE, navigateJobsPage, refreshDashboardData } = harness.hook;
  const delayedNavigation = deferred();
  seedSecondPage(UI_STATE);
  harness.setFetch((url) => {
    const value = String(url);
    if (value === "/v1/capabilities") {
      return Promise.resolve(jsonResponse({ ok: true }));
    }
    if (value === "/v1/system/storage") {
      return Promise.resolve(jsonResponse({ usage: {}, limits: {} }));
    }
    if (value.includes("cursor=cursor-2")) {
      return delayedNavigation.promise;
    }
    if (value.includes("cursor=cursor-1")) {
      return Promise.resolve(
        jsonResponse({ items: [job("refreshed-page-2")], next_cursor: "cursor-2" }),
      );
    }
    throw new Error(`unexpected fetch: ${value}`);
  });

  const navigation = navigateJobsPage(1);
  const refresh = refreshDashboardData({ cancelNavigation: true });
  assert.equal(await refresh, true);
  assert.equal(UI_STATE.jobsPageIndex, 1);
  assert.equal(UI_STATE.jobs[0].job_id, "refreshed-page-2");

  delayedNavigation.resolve(
    jsonResponse({ items: [job("stale-page-3")], next_cursor: null }),
  );
  assert.equal(await navigation, false);
  assert.equal(UI_STATE.jobs[0].job_id, "refreshed-page-2");
});


test("an automatic refresh waits for navigation and then reloads its page", async () => {
  const harness = createHarness();
  const { UI_STATE, navigateJobsPage, refreshDashboardData } = harness.hook;
  const delayedNavigation = deferred();
  let cursorTwoRequests = 0;
  seedSecondPage(UI_STATE);
  harness.setFetch((url) => {
    const value = String(url);
    if (value === "/v1/capabilities") {
      return Promise.resolve(jsonResponse({ ok: true }));
    }
    if (value === "/v1/system/storage") {
      return Promise.resolve(jsonResponse({ usage: {}, limits: {} }));
    }
    if (value.includes("cursor=cursor-2")) {
      cursorTwoRequests += 1;
      if (cursorTwoRequests === 1) {
        return delayedNavigation.promise;
      }
      return Promise.resolve(
        jsonResponse({ items: [job("refreshed-page-3")], next_cursor: "cursor-3" }),
      );
    }
    throw new Error(`unexpected fetch: ${value}`);
  });

  const navigation = navigateJobsPage(1);
  assert.equal(await refreshDashboardData(), false);
  assert.equal(UI_STATE.jobsRefreshPending, true);
  delayedNavigation.resolve(
    jsonResponse({ items: [job("page-3")], next_cursor: "cursor-3" }),
  );
  assert.equal(await navigation, true);
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(cursorTwoRequests, 2);
  assert.equal(UI_STATE.jobsPageIndex, 2);
  assert.equal(UI_STATE.jobs[0].job_id, "refreshed-page-3");
  assert.equal(UI_STATE.jobsRefreshPending, false);
});


test("a second automatic tick takes over a hung navigation", async () => {
  const harness = createHarness();
  const { UI_STATE, navigateJobsPage, refreshDashboardData } = harness.hook;
  const delayedNavigation = deferred();
  seedSecondPage(UI_STATE);
  harness.setFetch((url) => {
    const value = String(url);
    if (value === "/v1/capabilities") {
      return Promise.resolve(jsonResponse({ ok: true }));
    }
    if (value === "/v1/system/storage") {
      return Promise.resolve(jsonResponse({ usage: {}, limits: {} }));
    }
    if (value.includes("cursor=cursor-2")) {
      return delayedNavigation.promise;
    }
    if (value.includes("cursor=cursor-1")) {
      return Promise.resolve(
        jsonResponse({ items: [job("timer-refreshed-page-2")], next_cursor: "cursor-2" }),
      );
    }
    throw new Error(`unexpected fetch: ${value}`);
  });

  const navigation = navigateJobsPage(1);
  assert.equal(await refreshDashboardData(), false);
  assert.equal(UI_STATE.jobsRefreshPending, true);
  assert.equal(await refreshDashboardData(), true);
  assert.equal(UI_STATE.jobsPageIndex, 1);
  assert.equal(UI_STATE.jobs[0].job_id, "timer-refreshed-page-2");

  delayedNavigation.resolve(
    jsonResponse({ items: [job("stale-page-3")], next_cursor: null }),
  );
  assert.equal(await navigation, false);
  assert.equal(UI_STATE.jobs[0].job_id, "timer-refreshed-page-2");
});


test("old config and output responses are ignored after a token change", async () => {
  const harness = createHarness();
  const { UI_STATE, loadConfig, loadOutputs, setToken } = harness.hook;
  const delayedConfig = deferred();
  const delayedOutputs = deferred();
  const outputContainer = new FakeElement();

  setToken("token-a");
  harness.setFetch((url) => {
    if (String(url) === "/v1/system/config") {
      return delayedConfig.promise;
    }
    if (String(url).endsWith("/outputs")) {
      return delayedOutputs.promise;
    }
    throw new Error(`unexpected fetch: ${String(url)}`);
  });
  const configRequest = loadConfig();
  const outputRequest = loadOutputs("private-a", outputContainer);
  setToken("token-b");

  delayedConfig.resolve(jsonResponse({ revision: 9, editable: {}, readonly: {} }));
  delayedOutputs.resolve(jsonResponse({ files: [{ path: "private.txt", size_bytes: 1 }] }));
  assert.equal(await configRequest, false);
  assert.equal(await outputRequest, false);
  assert.equal(UI_STATE.config, null);
  assert.equal(outputContainer.children.length, 0);
  assert.notEqual(outputContainer.textContent, "private.txt");
});


test("an unauthorized refresh clears protected job config and storage data", async () => {
  const harness = createHarness();
  const { UI_STATE, refreshDashboardData, setToken } = harness.hook;
  setToken("expired-token");
  seedSecondPage(UI_STATE);
  UI_STATE.lastStorageFetch = { usage: { input_bytes: 1234 } };
  harness.setFetch(() => Promise.resolve(
    jsonResponse({ detail: "invalid token" }, 401),
  ));

  assert.equal(await refreshDashboardData(), false);
  assert.deepEqual(Array.from(UI_STATE.jobs), []);
  assert.equal(UI_STATE.config, null);
  assert.equal(UI_STATE.lastStorageFetch, null);
  assert.equal(harness.element("inputBytes").textContent, "-");
  assert.match(harness.element("serviceHealth").textContent, /需要 Token/);
});
