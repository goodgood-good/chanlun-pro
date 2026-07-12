'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadUtils(initialSelectedItems) {
  const storage = new Map();
  if (initialSelectedItems !== undefined) {
    storage.set('a_selectedItems', initialSelectedItems);
  }
  const localStorage = {
    getItem(key) { return storage.has(key) ? storage.get(key) : null; },
    setItem(key, value) { storage.set(key, String(value)); },
  };
  const layui = {
    data() { return { market: 'a' }; },
    use() {},
  };
  const sandbox = {
    console: { log() {}, warn() {}, error() {} },
    localStorage,
    layui,
    default_vals: { market: 'a' },
    JSON,
    Array,
    Set,
    Map,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;

  vm.createContext(sandbox);
  const source = fs.readFileSync(path.join(__dirname, '..', 'utils.js'), 'utf8');
  vm.runInContext(source + '\n;globalThis.__Utils = Utils;', sandbox, { filename: 'utils.js' });
  return { Utils: sandbox.__Utils, storage };
}

test('selected item cache safely falls back for malformed or non-array JSON', () => {
  for (const raw of ['{broken', '{"value":"not-an-array"}']) {
    const h = loadUtils(raw);
    assert.deepEqual(Array.from(h.Utils.get_selected_items()), []);
  }
});

test('add_to_cache replaces malformed storage with a valid recent item list', () => {
  const h = loadUtils('{broken');

  h.Utils.add_to_cache({ arr: [{ name: 'Alpha', value: 'a:alpha' }] });

  assert.deepEqual(JSON.parse(h.storage.get('a_selectedItems')), [
    { name: 'Alpha', value: 'a:alpha' },
  ]);
});

test('selected item cache drops malformed entries from an otherwise valid array', () => {
  const h = loadUtils('[null,{"name":"Valid","value":"a:valid"},{"name":"Missing value"}]');

  assert.deepEqual(
    Array.from(h.Utils.get_selected_items(), (item) => ({ ...item })),
    [{ name: 'Valid', value: 'a:valid' }],
  );
  assert.doesNotThrow(() => {
    h.Utils.add_to_cache({ arr: [{ name: 'New', value: 'a:new' }] });
  });
});

test('watchlist search uses the safe selected item reader', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'zixuan.js'), 'utf8');

  assert.doesNotMatch(source, /JSON\.parse\(localStorage\.getItem/);
  assert.equal((source.match(/Utils\.get_selected_items\(\)/g) || []).length, 2);
});
