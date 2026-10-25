// Use Paseo's configuration API; removing a plugin would also remove its settings.
import { readFileSync, realpathSync } from 'node:fs';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

export async function activate(client, id, directory) {
  return (await activateBatch(client, { [id]: directory }))[0];
}

export async function activateBatch(client, directories) {
  const { config } = await client.getDaemonConfig();
  const previous = {};
  for (const id of Object.keys(directories)) {
    const source = config.plugins?.[id];
    if (!source || source.source !== 'directory' || source.enabled === false)
      throw new Error(`Expected an enabled directory plugin: ${id}`);
    previous[id] = source;
  }
  async function setSources(sources) {
    const { config: current } = await client.getDaemonConfig();
    await client.patchDaemonConfig({ plugins: { ...current.plugins, ...sources } });
  }
  async function reload(id) {
    const plugin = await client.reloadPlugin(id);
    if (plugin.status !== 'running') throw new Error(plugin.error || `Plugin ${id} ${plugin.status}`);
    return plugin;
  }
  try {
    // A reload response reads the whole plugin catalog. Move all selected paths
    // before reloading so another old GPFS source cannot hold up the migration.
    await setSources(Object.fromEntries(Object.entries(directories).map(
      ([id, path]) => [id, { ...previous[id], path }])));
    const results = [];
    for (const id of Object.keys(directories)) results.push(await reload(id));
    return results;
  } catch (error) {
    try {
      await setSources(previous);
      for (const id of Object.keys(previous)) await reload(id);
    } catch (rollbackError) {
      throw new AggregateError([error, rollbackError], 'Plugin activation and rollback failed; inspect plugin status');
    }
    throw error;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href) {
  if (process.argv.length !== 6) throw new Error('Usage: activate <cli-package> <home> <plugin-id|--batch> <directory|json-file>');
  const [cli, home, id, directory] = process.argv.slice(2);
  const require = createRequire(join(cli, 'package.json'));
  const { createPaseoClient } = await import(pathToFileURL(require.resolve('@getpaseo/client')));
  const { listen } = JSON.parse(readFileSync(join(home, 'paseo.pid'), 'utf8'));
  const client = createPaseoClient({
    url: listen.startsWith('/') ? `ws+unix://${listen}:/ws` : `ws://${listen}/ws`,
    clientId: `plugin-deploy.${process.pid}`, clientType: 'cli',
    localCredential: () => readFileSync(join(home, 'local-credential'), 'utf8').trim(),
    reconnect: { enabled: false },
  });
  try {
    await client.connect();
    const operations = {
      getDaemonConfig: () => client.config.get(),
      patchDaemonConfig: patch => client.config.patch(patch),
      reloadPlugin: async pluginId => {
        const { stdout } = await promisify(execFile)(process.execPath,
          [join(cli, 'bin/paseo'), 'plugin', 'reload', pluginId, '--home', home, '--json']);
        return JSON.parse(stdout);
      },
    };
    console.log(JSON.stringify(id === '--batch'
      ? await activateBatch(operations, JSON.parse(readFileSync(directory, 'utf8')))
      : await activate(operations, id, directory)));
  } finally {
    await client.close();
  }
}
