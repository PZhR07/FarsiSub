"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function editor() {
  const elements = new Map(), requests = [];
  function element() {
    return {
      disabled: false, textContent: "", value: "", hidden: false, src: "", dataset: {}, children: [],
      classList: {toggle() {}},
      addEventListener(event, handler) { this[event] = handler; },
      setAttribute() {}, append(...nodes) { this.children.push(...nodes); },
      replaceChildren(...nodes) { this.children = nodes; }, pause() {}, play() { return Promise.resolve(); }
    };
  }
  function get(id) {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  }
  const context = vm.createContext({
    document: {getElementById: get, querySelector: () => ({content: "token"}),
      createElement: element, querySelectorAll: () => []},
    window: {addEventListener() {}, confirm: () => true},
    localStorage: {setItem() {}}, Date, Set, Map,
    fetch(url, options) {
      return new Promise((resolve, reject) => requests.push({url, options, resolve, reject}));
    }
  });
  const code = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8").split("\n(async () => {")[0];
  vm.runInContext(code, context);
  const run = code => vm.runInContext(code, context);
  run(`selected = "A"; loaded = "A"; cueRevision = '"v1"'; rows = [{id:1, start:0, end:1, english:"Hello", persian:"first"}]; baseRows=copyRows(rows); baseRows[0].persian="original"; renderRows(); markDirty(true);`);
  const state = () => JSON.parse(run("JSON.stringify({selected, rows, dirty, saving, cueRevision, conflictCount:conflicts.size})"));
  const input = text => {
    const textarea = get("cue-list").children[0].children[2];
    textarea.value = text; textarea.input();
  };
  const reply = (request, text = "first", etag = '"v2"', ok = true) => request.resolve({
    ok, status: ok ? 200 : 409, headers: {get: () => etag},
    json: async () => ok ? [{id:1, start:0, end:1, english:"Hello", persian:text}] : {error:"conflict"}
  });
  return {get, requests, run, state, input, reply};
}

test("typing during save remains dirty and the next save uses the accepted revision", async () => {
  const e = editor();
  const first = e.get("save").click();
  e.input("second");
  assert.equal(e.get("save").disabled, true);
  e.reply(e.requests[0]); await first;
  assert.equal(e.state().rows[0].persian, "second");
  assert.equal(e.state().dirty, true);
  assert.equal(e.get("save").disabled, false);
  const second = e.get("save").click();
  assert.equal(e.requests[1].options.headers["If-Match"], '"v2"');
  assert.equal(JSON.parse(e.requests[1].options.body)[0].persian, "second");
  e.reply(e.requests[1], "second", '"v3"'); await second;
  assert.equal(e.state().dirty, false);
});

test("input handlers still modify current rows after a successful save", async () => {
  const e = editor(), saving = e.get("save").click();
  e.reply(e.requests[0], "normalized"); await saving;
  e.input("third");
  assert.equal(e.state().rows[0].persian, "third");
  assert.equal(e.state().dirty, true);
});

test("late save response cannot replace another selected job", async () => {
  const e = editor(), saving = e.get("save").click();
  e.run(`selectionEpoch++; selected="B"; loaded="B"; rows=[{id:1,persian:"B text"}]; cueRevision='"B1"'; markDirty(false);`);
  e.reply(e.requests[0]); await saving;
  assert.equal(e.state().rows[0].persian, "B text");
  assert.equal(e.state().cueRevision, '"B1"');
  assert.equal(e.state().dirty, false);
  assert.equal(e.get("subtitle-track").src, "");
});

test("returning to the same job does not let an earlier session overwrite its new data", async () => {
  const e = editor(), saving = e.get("save").click();
  e.run(`selectionEpoch+=2; rows=[{id:1,persian:"reloaded"}]; cueRevision='"fresh"'; markDirty(false);`);
  e.reply(e.requests[0]); await saving;
  assert.equal(e.state().rows[0].persian, "reloaded");
  assert.equal(e.state().cueRevision, '"fresh"');
});

test("late save errors do not mark another job dirty", async () => {
  const e = editor(), saving = e.get("save").click();
  e.run(`selectionEpoch++; selected="B"; rows=[{id:1,persian:"B text"}]; markDirty(false);`);
  e.reply(e.requests[0], "", "", false); await saving;
  assert.equal(e.state().dirty, false);
  assert.equal(e.get("form-error").textContent, "");
});

test("duplicate save clicks cannot send concurrent requests", async () => {
  const e = editor(), saving = e.get("save").click();
  e.input("second"); await e.get("save").click();
  assert.equal(e.requests.length, 1);
  e.reply(e.requests[0]); await saving;
});

test("conflict response fetches server data and preserves text for explicit resolution", async () => {
  const e = editor(), saving = e.get("save").click();
  e.reply(e.requests[0], "", "", false);
  await new Promise(setImmediate);
  e.reply(e.requests[1], "remote", '"v2"'); await saving;
  assert.equal(e.state().rows[0].persian, "first");
  assert.equal(e.state().dirty, true);
  assert.equal(e.get("save").disabled, false);
  assert.equal(e.state().conflictCount, 1);
  assert.equal(e.state().cueRevision, '"v2"');
  const blocked = e.get("save").click(); await blocked;
  assert.equal(e.requests.length, 2);
  const block = e.get("cue-list").children[0];
  const choices = block.children[block.children.length - 1];
  choices.children[0].click();
  assert.equal(e.state().conflictCount, 0);
  const retried = e.get("save").click();
  assert.equal(e.requests[2].options.headers["If-Match"], '"v2"');
  e.reply(e.requests[2], "first", '"v3"'); await retried;
  assert.equal(e.state().dirty, false);
});

test("lost success reply is recovered without losing newer typing", async () => {
  const e = editor(), saving = e.get("save").click();
  e.input("second");
  e.requests[0].reject(new TypeError("network interrupted"));
  await new Promise(setImmediate);
  e.reply(e.requests[1], "first", '"v2"'); await saving;
  assert.equal(e.state().rows[0].persian, "second");
  assert.equal(e.state().dirty, true);
  assert.equal(e.state().conflictCount, 0);
  const retried = e.get("save").click();
  assert.equal(e.requests[2].options.headers["If-Match"], '"v2"');
  e.reply(e.requests[2], "second", '"v3"'); await retried;
  assert.equal(e.state().dirty, false);
});

test("a later recovery keeps the original uncertain submission", async () => {
  const e = editor(), saving = e.get("save").click();
  e.requests[0].reject(new TypeError("offline"));
  await new Promise(setImmediate);
  e.requests[1].reject(new TypeError("still offline")); await saving;
  e.input("second");
  const recovery = e.get("save").click();
  assert.equal(e.requests[2].options.method, undefined);
  e.reply(e.requests[2], "first", '"v2"'); await recovery;
  assert.equal(e.state().rows[0].persian, "second");
  assert.equal(e.state().conflictCount, 0);
  assert.equal(e.state().cueRevision, '"v2"');
});

test("independent edits from another tab are merged without conflicts", async () => {
  const e = editor();
  e.run(`rows=[{id:1,start:0,end:1,english:"One",persian:"local"},{id:2,start:2,end:3,english:"Two",persian:"base two"}]; baseRows=copyRows(rows);baseRows[0].persian="base one";renderRows();`);
  const saving = e.get("save").click();
  e.reply(e.requests[0], "", "", false);
  await new Promise(setImmediate);
  e.requests[1].resolve({ok:true,headers:{get:()=> '"v2"'},json:async()=>[
    {id:1,start:0,end:1,english:"One",persian:"base one"},
    {id:2,start:2,end:3,english:"Two",persian:"remote"}]});
  await saving;
  assert.equal(e.state().rows[0].persian, "local");
  assert.equal(e.state().rows[1].persian, "remote");
  assert.equal(e.state().conflictCount, 0);
});

test("large subtitle lists render only one page and edits survive navigation", () => {
  const e = editor();
  e.run(`rows=Array.from({length:10000},(_,i)=>({id:i+1,start:i*2,end:i*2+1,english:"English",persian:"Text"}));baseRows=copyRows(rows);renderRows();`);
  assert.equal(e.get("cue-list").children.length, 40);
  e.input("changed");
  e.get("page-next").click();
  assert.equal(e.get("cue-list").children.length, 40);
  assert.equal(e.get("cue-list").children[0].dataset.id, 41);
  e.get("page-prev").click();
  assert.equal(e.get("cue-list").children[0].children[2].value, "changed");
  e.get("search").value = "changed"; e.get("search").input();
  assert.equal(e.get("cue-list").children.length, 1);
});
