import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// jsdom has no layout, so it has no `ResizeObserver` -- and assistant-ui's
// viewport attaches one to keep a growing transcript scrolled. Observing
// nothing is the honest stand-in: there are no sizes here to report.
if (!("ResizeObserver" in globalThis)) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

// Same reason: nothing here scrolls, so the viewport's request to is a no-op
// rather than the missing method jsdom otherwise reports mid-render.
if (!Element.prototype.scrollTo) {
  Element.prototype.scrollTo = () => {};
}

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});
