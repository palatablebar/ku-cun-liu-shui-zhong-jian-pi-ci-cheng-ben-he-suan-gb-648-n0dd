/* 页面交互：重跑、盘点提交、失败提示。状态集中在这里，不散落全局变量。 */
(function () {
  "use strict";

  const state = { busy: false };

  function $(selector) {
    return document.querySelector(selector);
  }

  function note(element, text, kind) {
    if (!element) {
      return;
    }
    element.textContent = text;
    element.hidden = false;
    element.classList.remove("notice-bad", "notice-ok");
    if (kind === "bad") {
      element.classList.add("notice-bad");
    }
    if (kind === "ok") {
      element.classList.add("notice-ok");
    }
  }

  async function requestJson(url, options) {
    const response = await fetch(url, Object.assign({ headers: { "Content-Type": "application/json" } }, options || {}));
    let payload = null;
    try {
      payload = await response.json();
    } catch (error) {
      payload = null;
    }
    if (!response.ok) {
      const message = payload && payload.message ? payload.message : "请求失败 " + response.status;
      const code = payload && payload.error ? payload.error : "E_HTTP";
      throw new Error(code + " · " + message);
    }
    return payload;
  }

  function bindDemo() {
    const button = $("#run-demo");
    if (!button) {
      return;
    }
    button.addEventListener("click", async function () {
      if (state.busy) {
        return;
      }
      state.busy = true;
      button.disabled = true;
      button.textContent = "运行中";
      try {
        await requestJson("/api/demo/run", { method: "POST" });
        window.location.reload();
      } catch (error) {
        note($("#log-note") || $("#count-note"), String(error.message || error), "bad");
        button.disabled = false;
        button.textContent = "重跑";
      } finally {
        state.busy = false;
      }
    });
  }

  function bindCountForm() {
    const form = $("#count-form");
    if (!form) {
      return;
    }
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      const box = $("#count-note");
      const data = new FormData(form);
      const body = {
        count_no: String(data.get("count_no") || ""),
        revision: Number(data.get("revision") || 0),
        rows: [
          {
            sku: String(data.get("sku") || ""),
            warehouse: String(data.get("warehouse") || ""),
            counted_qty: String(data.get("counted_qty") || ""),
            unit: "kg"
          }
        ]
      };
      try {
        const result = await requestJson("/api/reconcile/counts", {
          method: "POST",
          body: JSON.stringify(body)
        });
        note(box, "已记录 · 盘点行 " + result.counts.length, "ok");
        window.setTimeout(function () {
          window.location.reload();
        }, 400);
      } catch (error) {
        note(box, String(error.message || error), "bad");
      }
    });
  }

  function bindPatch() {
    const button = $("#run-patch");
    if (!button) {
      return;
    }
    button.addEventListener("click", async function () {
      if (state.busy) {
        return;
      }
      state.busy = true;
      button.disabled = true;
      button.textContent = "采信中";
      try {
        const form = new FormData();
        form.append("sample", "patch_day1.csv");
        const response = await fetch("/api/movements/patch", { method: "POST", body: form });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error((payload && payload.error ? payload.error : "E_HTTP") + " · " + (payload && payload.message ? payload.message : "请求失败"));
        }
        window.location.reload();
      } catch (error) {
        note($("#log-note"), String(error.message || error), "bad");
        button.disabled = false;
        button.textContent = "补丁样例";
      } finally {
        state.busy = false;
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    bindDemo();
    bindPatch();
    bindCountForm();
  });
})();
