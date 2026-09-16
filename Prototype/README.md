# Welcome to your Lovable project

This project was built with [Lovable](https://lovable.dev).

## Build with Lovable

Open your project in the [Lovable editor](https://lovable.dev) and keep building.

- **Ship faster**: describe what you want to build and Lovable handles the code.
- **Stay in sync**: connect the project to GitHub and every change made in Lovable is committed straight to your repository.
- **Full ownership**: this code is yours. Push to your repository and your changes sync back into Lovable, ready for your next prompt.

## Development

Prefer working locally? You need Node.js and npm — [install with nvm](https://github.com/nvm-sh/nvm#installing-and-updating).

```sh
git clone <this-repository-url>
cd <repository-name>
npm i
npm run dev
```

## Built with

- TanStack Start
- TypeScript
- React
- Tailwind CSS

## Status in this repository (backend integration)

This Prototype is a Lovable-generated **design mock**. It makes no calls to the Python backend in
`travel_agent/`: the intake and the "four specialists" are TanStack server functions that parse text with
regular expressions and rank a hard-coded catalogue of 40-odd places for Berlin, Paris and Lisbon
(`src/lib/travel/data.ts`), and `src/lib/travel/planner.ts` computes times and distances client-side.
The shipped UI is `travel_agent/web/`, served by the FastAPI app at `/`.

See [`BACKEND_INTEGRATION.md`](./BACKEND_INTEGRATION.md) for how to point this UI at the real backend
(base URL, dev proxy, endpoints, the SSE job feed) and for a component-by-component mapping of the mock
data to the real API fields. Note: `npm ci` fails on this lockfile with `Invalid Version:` because
`package.json` has no `version` field while `overrides` is set; use `npm install` (which regenerates the
lock) or add a `"version"` line.
