# Vendored browser dependencies

These files are self-hosted so the Nostr login flow does not execute third-party CDN code at runtime.

- `nostr-tools-nip46-2.24.1.mjs`
  - Source: `https://esm.sh/nostr-tools@2.24.1/es2022/nip46.bundle.mjs`
  - SHA-256: `578c99c82410d670afa2e3e2814f84bafeeea356b7ed020793f19c5ee763f506`
  - Upstream license: MIT
- `nostr-tools-pure-2.24.1.mjs`
  - Source: `https://esm.sh/nostr-tools@2.24.1/es2022/pure.bundle.mjs`
  - SHA-256: `a2622aaa6afda0a521aea05c9c0f9a1d475e4887c31ffbf22a6a56c0e6020f39`
  - Upstream license: MIT
- `qrcode-generator-1.4.4.js`
  - Source: `https://unpkg.com/qrcode-generator@1.4.4/qrcode.js`
  - SHA-256: `18ae399f81182bc9de916e9c77b195df20cc58d6f2d55a62b085a299f1bf1780`
  - Upstream license: MIT

When updating a dependency, pin the version, replace the file, update its hash here, and rerun the browser and authentication test suites.
