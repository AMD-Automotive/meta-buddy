/*
 * Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
 * SPDX-License-Identifier: MIT
 * Contributors: KC
 * Maintainer: KC
 */

// Run with: node --test recipes-apps/rave-menu/tests/test_app_confirm.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const appSource = fs.readFileSync(
  path.join(__dirname, "../files/static/app.js"), "utf8"
);
const confirmSource = appSource.split("const customUrlCard", 1)[0];

class Element {
  constructor() {
    this.listeners = {};
    this.textContent = "";
    const classes = new Set(["hidden"]);
    this.classList = {
      add: name => classes.add(name),
      remove: name => classes.delete(name),
      contains: name => classes.has(name),
    };
  }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  removeEventListener(name) { delete this.listeners[name]; }
  focus() {}
}

function harness() {
  const elements = Object.fromEntries(
    ["confirmModal", "confirmMessage", "confirmTitle", "confirmOk", "confirmCancel"]
      .map(id => [id, new Element()])
  );
  const translations = {
    "Please confirm": "Επιβεβαίωση",
    "Continue": "Συνέχεια",
    "Cancel": "Άκυρο",
    "Reboot": "Επανεκκίνηση",
    "Reboot the Versal device?": "Επανεκκίνηση της συσκευής Versal;",
  };
  const context = vm.createContext({
    document: {
      getElementById: id => elements[id] || null,
      addEventListener() {},
      removeEventListener() {},
    },
    fetch: async () => ({ json: async () => ({ state: "ok" }) }),
    requestAnimationFrame: callback => callback(),
  });
  context.window = context;
  const catalogueTranslator = key => translations[key] || key;
  context.window.t = catalogueTranslator;
  context.window.confirm = () => false;
  vm.runInContext(confirmSource, context);
  return { context, elements, catalogueTranslator };
}

test("app.js preserves the catalogue translator and opens a translated confirm dialog", async () => {
  const { context, elements, catalogueTranslator } = harness();

  assert.equal(context.window.t, catalogueTranslator);
  assert.equal(context.t, catalogueTranslator);

  const result = vm.runInContext(
    'showConfirm(translate("Reboot the Versal device?"), { okLabel: translate("Reboot") })',
    context
  );

  assert.equal(elements.confirmModal.classList.contains("hidden"), false);
  assert.equal(elements.confirmTitle.textContent, "Επιβεβαίωση");
  assert.equal(elements.confirmMessage.textContent, "Επανεκκίνηση της συσκευής Versal;");
  assert.equal(elements.confirmOk.textContent, "Επανεκκίνηση");
  assert.equal(elements.confirmCancel.textContent, "Άκυρο");

  elements.confirmCancel.listeners.click();
  assert.equal(await result, false);
  assert.equal(elements.confirmModal.classList.contains("hidden"), true);
});