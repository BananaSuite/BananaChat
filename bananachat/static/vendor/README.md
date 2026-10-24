# Vendored browser libraries

Served as they are to the administrator's worker terminal and VNC viewer
(`templates/admin/worker_terminal.html`, `worker_vnc.html`). Each is one ES
module bundled with esbuild 0.25.10 from the npm packages below, so the pages
need no CDN and keep the strict Content-Security-Policy.

| File | Package | Licence |
| --- | --- | --- |
| `xterm/xterm.esm.js`, `xterm/xterm.css` | `@xterm/xterm` 5.5.0 and `@xterm/addon-fit` 0.10.0 | MIT (`xterm/LICENSE.txt`) |
| `novnc/novnc.esm.js` | `@novnc/novnc` 1.5.0 (with its bundled pako) | MPL-2.0 (`novnc/LICENSE.txt`, `novnc/LICENSE.MPL-2.0`); pako MIT |

Rebuild (from an empty folder):

```sh
npm install @xterm/xterm@5.5.0 @xterm/addon-fit@0.10.0 @novnc/novnc@1.5.0 esbuild@0.25.10
printf 'export { Terminal } from "@xterm/xterm";\nexport { FitAddon } from "@xterm/addon-fit";\n' > xterm.js
printf 'export { default as RFB } from "@novnc/novnc/lib/rfb.js";\n' > novnc.js
npx esbuild xterm.js --bundle --format=esm --minify --target=es2020 --legal-comments=eof --outfile=xterm.esm.js
npx esbuild novnc.js --bundle --format=esm --minify --target=es2020 --outfile=novnc.esm.js
```

noVNC 1.6 and 1.7 publish CommonJS with a top-level `await` that cannot be
bundled; stay on 1.5.0 until the package is fixed. The source code of the
MPL-2.0 files is the published package itself.
