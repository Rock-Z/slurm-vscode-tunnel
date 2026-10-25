import { test } from 'node:test';
import assert from 'node:assert/strict';
import { activate } from '../paseo_plugin_activate.mjs';

test('failed activation restores the old source without removing settings', async () => {
  let plugins = { demo: { source: 'directory', path: '/old', enabled: true }, other: { path: '/other' } };
  const client = {
    getDaemonConfig: async () => ({ config: { plugins: structuredClone(plugins) } }),
    patchDaemonConfig: async (patch) => { plugins = patch.plugins; },
    reloadPlugin: async () => ({ status: plugins.demo.path === '/new' ? 'failed' : 'running' }),
  };
  await assert.rejects(activate(client, 'demo', '/new'), /failed/);
  assert.equal(plugins.demo.path, '/old');
  assert.equal(plugins.other.path, '/other');
});

test('successful activation preserves other plugin configuration', async () => {
  let plugins = { demo: { source: 'directory', path: '/old', enabled: true }, other: { path: '/other' } };
  const client = {
    getDaemonConfig: async () => ({ config: { plugins: structuredClone(plugins) } }),
    patchDaemonConfig: async (patch) => { plugins = patch.plugins; },
    reloadPlugin: async () => ({ status: 'running', path: plugins.demo.path }),
  };
  assert.equal((await activate(client, 'demo', '/new')).status, 'running');
  assert.equal(plugins.demo.path, '/new');
  assert.equal(plugins.other.path, '/other');
});

test('batch changes every source before catalog-dependent reloads', async () => {
  const { activateBatch } = await import('../paseo_plugin_activate.mjs');
  let plugins = Object.fromEntries(['a', 'b'].map(id => [id, { source: 'directory', path: `/gpfs/${id}`, enabled: true }]));
  const client = {
    getDaemonConfig: async () => ({ config: { plugins: structuredClone(plugins) } }),
    patchDaemonConfig: async patch => { plugins = patch.plugins; },
    reloadPlugin: async () => {
      assert.ok(Object.values(plugins).every(p => p.path.startsWith('/tmp/')));
      return { status: 'running' };
    },
  };
  assert.equal((await activateBatch(client, { a: '/tmp/a', b: '/tmp/b' })).length, 2);
});

test('CLI entry runs when invoked through the Apps symlink', async () => {
  const { mkdtempSync, symlinkSync, rmSync } = await import('node:fs');
  const { spawnSync } = await import('node:child_process');
  const dir = mkdtempSync('/tmp/plugin-entry-test-');
  try {
    symlinkSync(new URL('../paseo_plugin_activate.mjs', import.meta.url).pathname, `${dir}/activate.mjs`);
    const result = spawnSync(process.execPath, [`${dir}/activate.mjs`], { encoding: 'utf8' });
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /Usage: activate/);
  } finally { rmSync(dir, { recursive: true }); }
});
