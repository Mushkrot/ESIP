# ESIP web workspace

The main client is a React 19 and TypeScript application for ticket investigation, Universal Chat,
analytics, knowledge-gap analysis, reporting, and administration.

## Local development

```bash
npm ci
npm run dev
```

Vite listens on `http://127.0.0.1:3000` and proxies `/api` to `http://127.0.0.1:8000`. The default
`reference` profile signs in with the included Northstar administrator and needs no Firebase project.

## Checks

```bash
npm run lint
npm run type-check
npm run build
```

The production build is written to `dist/`. See the root [README](../README.md) for the system map and
[SETUP](../SETUP.md) for the full local stack.
