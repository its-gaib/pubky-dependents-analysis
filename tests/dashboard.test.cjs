const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const readJSON = (file) => JSON.parse(readFileSync(path.join(root, file), 'utf8'));
const crates = readJSON('crates.json');
const html = readFileSync(path.join(root, 'docs/index.html'), 'utf8');
const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)];
const pageURL = 'https://its-gaib.github.io/pubky-dependents-analysis/';

async function dashboard({ url = pageURL, fetchResponse } = {}) {
  const buttons = [];
  const elements = {
    'gh-link': {},
    content: { innerHTML: '' },
    tabs: {
      appendChild: (button) => buttons.push(button),
      querySelectorAll: () => buttons,
    },
  };
  const requests = [];
  const context = vm.createContext({
    location: new URL(url),
    URL,
    document: {
      getElementById(id) {
        assert.ok(html.includes(`id="${id}"`), `Missing HTML element: ${id}`);
        assert.ok(elements[id], `Unexpected DOM access: ${id}`);
        return elements[id];
      },
      createElement(tag) {
        assert.equal(tag, 'button');
        const button = { dataset: {} };
        button.classList = { toggle: (_, active) => { button.active = active; } };
        return button;
      },
    },
    async fetch(request) {
      requests.push(request);
      if (fetchResponse) return fetchResponse(request);
      const file = new URL(request).pathname.split('/').pop();
      return { ok: true, json: async () => readJSON(`docs/${file}`) };
    },
  });
  assert.ok(scripts.length, 'The dashboard must contain executable scripts');
  for (const [, script] of scripts) {
    // Run the actual inline scripts, including the initial asynchronous load.
    await vm.runInContext(script, context, { filename: 'docs/index.html' });
  }
  return {
    ...vm.runInContext('({ CRATES, CRATE_META, load, render })', context),
    content: elements.content,
    buttons,
    requests,
  };
}

function assertRendered(content, data) {
  assert.match(content.innerHTML, /How projects depend on/);
  assert.ok(content.innerHTML.includes(`${data.total.toLocaleString()}</div>`));
  assert.ok(content.innerHTML.includes(`href="${pageURL}${data.crate}.json"`));
  assert.doesNotMatch(content.innerHTML, /NaN|Infinity|undefined|Invalid Date/);
}

test('dashboard tabs and package links agree with crates.json', async () => {
  const app = await dashboard();
  const names = crates.map(({ crate }) => crate);
  assert.deepEqual(Array.from(app.CRATES), names);
  assert.deepEqual(Object.keys(app.CRATE_META).sort(), [...names].sort());
  assert.deepEqual(app.buttons.map((button) => button.dataset.crate), names);
  for (const config of crates) {
    const meta = app.CRATE_META[config.crate];
    assert.equal(meta.github, config.github_repo);
    assert.equal(meta.npm, config.npm_package);
    assert.equal(meta.rn, config.react_native_package);
  }
});

for (const { crate } of crates) {
  test(`the ${crate} tab renders its published snapshot`, async () => {
    const app = await dashboard();
    await app.buttons.find((button) => button.dataset.crate === crate).onclick();
    assertRendered(app.content, readJSON(`docs/${crate}.json`));
    assert.deepEqual(
      app.buttons.filter((button) => button.active).map((button) => button.dataset.crate),
      [crate],
    );
    const requestCount = app.requests.length;
    await app.load(crate);
    assert.equal(app.requests.length, requestCount, 'Reopening a tab uses cached data');
  });
}

test('render tolerates missing optional fields and unavailable star counts', async () => {
  const app = await dashboard();
  const data = {
    crate: crates[0].crate,
    updated_at: '2026-04-01T00:00:00Z',
    total: 2,
    summary: { direct: 2 },
    lists: { direct: [{ repo: 'example/one', stars: null }, { repo: 'example/two' }] },
  };
  app.render(data);
  assertRendered(app.content, data);
  assert.match(app.content.innerHTML, /example\/one/);
  assert.match(app.content.innerHTML, /example\/two/);
  assert.doesNotMatch(app.content.innerHTML, /downloads \(all-time\)/);
});

test('render supports a snapshot with no dependents', async () => {
  const app = await dashboard();
  const data = {
    crate: crates[0].crate,
    updated_at: '2026-04-01T00:00:00Z',
    total: 0,
    summary: {},
    lists: {},
  };
  app.render(data);
  assertRendered(app.content, data);
});

test('load reports HTTP and network failures and permits retrying', async () => {
  let attempt = 0;
  const data = readJSON(`docs/${crates[0].crate}.json`);
  const app = await dashboard({
    fetchResponse: async () => {
      attempt += 1;
      if (attempt === 1) return { ok: false, status: 404 };
      if (attempt === 2) throw new Error('Network unavailable');
      return { ok: true, json: async () => data };
    },
  });
  assert.match(app.content.innerHTML, /Failed to load .*\.json \(404\)/);
  await app.load(data.crate);
  assert.match(app.content.innerHTML, /Error: Network unavailable/);
  await app.load(data.crate);
  assertRendered(app.content, data);
  assert.equal(attempt, 3);
});

for (const suffix of ['', 'index.html', 'index.html?source=https://example.org/#details/']) {
  test(`snapshot URLs resolve next to the dashboard at ${suffix || '/'}`, async () => {
    const app = await dashboard({ url: pageURL + suffix });
    assert.equal(app.requests[0], `${pageURL}${crates[0].crate}.json`);
    assertRendered(app.content, readJSON(`docs/${crates[0].crate}.json`));
  });
}
