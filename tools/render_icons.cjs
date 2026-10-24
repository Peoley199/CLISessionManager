// Development-only asset build; runtime remains Python standard-library only.
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const requireBuild = createRequire(path.join(process.env.TEMP, 'cli-session-manager-artwork', 'package.json'));
const { Resvg } = requireBuild('@resvg/resvg-js');
const { PNG } = requireBuild('pngjs');

async function main() {
  const names = ['x', 'refresh-cw', 'panel-right', 'settings-2', 'filter-x', 'square-pen',
                 'tags', 'folder-open', 'copy', 'trash-2', 'arrow-up-to-line', 'arrow-down-to-line'];
  const sizes = [16, 20, 24, 28, 32, 40, 48];
  const sheet = new PNG({width: names.length * 48, height: sizes.reduce((a, b) => a + b, 0)});
  const manifest = {};
  for (const [i, name] of names.entries()) {
    const response = await fetch(`https://unpkg.com/lucide-static@0.468.0/icons/${name}.svg`);
    if (!response.ok) throw new Error(`${name}: HTTP ${response.status}`);
    const svg = (await response.text()).replaceAll('currentColor', '#39454f');
    manifest[name] = {};
    let y = 0;
    for (const size of sizes) {
      const icon = PNG.sync.read(new Resvg(svg, {fitTo: {mode: 'width', value: size}}).render().asPng());
      PNG.bitblt(icon, sheet, 0, 0, size, size, i * 48, y);
      manifest[name][size] = [i * 48, y, size];
      y += size;
    }
  }
  const assets = path.join(__dirname, '..', 'assets');
  fs.mkdirSync(assets, {recursive: true});
  fs.writeFileSync(path.join(assets, 'icons.png'), PNG.sync.write(sheet));
  fs.writeFileSync(path.join(assets, 'icons.json'), JSON.stringify(manifest, null, 2) + '\n');
  const license = await fetch('https://unpkg.com/lucide-static@0.468.0/LICENSE');
  if (!license.ok) throw new Error(`License: HTTP ${license.status}`);
  fs.writeFileSync(path.join(assets, 'lucide-LICENSE'), await license.text());
  console.log(`Rendered ${names.length} Lucide icons at ${sizes.length} sizes.`);
}
main().catch(error => { console.error(error); process.exitCode = 1; });
