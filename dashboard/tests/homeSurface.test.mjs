/**
 * Structural guards for the deployed showcase page.
 *
 * The bug this file exists for: `dashboard/src/index.css` pinned the document
 * with `body { overflow: hidden }`. <html> is `overflow: visible`, so body's
 * value propagates to the viewport and the ~3700px product overview could not be
 * scrolled by wheel or touch on https://uc.jameryw.dev (measured with CDP wheel
 * events: scrollY stayed 0). The page also mounted `useGrpcWeb` unconditionally,
 * so the static showcase retried a Gateway stream that does not exist there.
 *
 * These assertions read the sources rather than a browser so they run in the
 * normal `node --test` job and fail the moment either regression returns.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const css = readFileSync(join(here, "..", "src", "index.css"), "utf8");
const homePage = readFileSync(join(here, "..", "src", "pages", "HomePage.tsx"), "utf8");

/** Body of the first rule whose selector list is exactly `selector`. */
function ruleBody(source, selector) {
  const marker = `${selector} {`;
  const start = source.indexOf(`\n${marker}`);
  assert.notEqual(start, -1, `missing rule: ${selector}`);
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    const char = source[index];
    if (char === "{") depth += 1;
    else if (char === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(open + 1, index);
    }
  }
  throw new Error(`unterminated rule: ${selector}`);
}

test("the document is scrollable: body must not pin the viewport", () => {
  const body = ruleBody(css, "body");
  assert.match(body, /margin: 0/, "matched the wrong rule");
  assert.doesNotMatch(body, /overflow:\s*hidden/, "body must not pin the viewport (breaks wheel/touch scrolling)");
  assert.doesNotMatch(body, /overflow-y:\s*hidden/, "body must not pin the viewport vertically");
  assert.match(body, /overflow-x:\s*hidden/, "sideways overflow should still be clamped");
});

test("the operations shell still pins the document for itself", () => {
  const shell = ruleBody(css, "body:has(.terminal-app)");
  assert.match(shell, /overflow:\s*hidden/, "the terminal shell relies on a pinned document");
});

test(".home-page scrolls with the document instead of trying to be the scroller", () => {
  const page = ruleBody(css, ".home-page");
  assert.doesNotMatch(page, /overflow-y:\s*auto/, "a content-sized element cannot be its own scroller");
  assert.doesNotMatch(page, /overflow:\s*hidden/, "overflow: hidden would clip the sticky header");
  assert.match(page, /overflow-x:\s*clip/, "clip keeps the glows in without creating a scroll container");
  assert.match(page, /min-height:\s*100dvh/, "mobile browsers need the dynamic viewport unit");
});

test("smooth scrolling is declared on the element that actually scrolls", () => {
  const page = ruleBody(css, ".home-page");
  assert.doesNotMatch(page, /scroll-behavior/, "scroll-behavior on a non-scrolling element does nothing");
  const html = ruleBody(css, "html:has(.home-page)");
  assert.match(html, /scroll-behavior:\s*smooth/);
});

test("the sticky showcase header, progress bar and skip link exist", () => {
  const topbar = ruleBody(css, ".home-topbar");
  assert.match(topbar, /position:\s*sticky/);
  assert.match(topbar, /top:\s*0/);
  assert.match(ruleBody(css, ".home-scroll-progress"), /transform:\s*scaleX\(0\)/);
  assert.match(ruleBody(css, ".home-skip-link"), /position:\s*fixed/);
  // Anchored sections must clear the sticky header.
  const section = ruleBody(css, ".home-section");
  const margin = /scroll-margin-top:\s*(\d+)px/.exec(section);
  assert.ok(margin, "sections need a scroll-margin-top");
  assert.ok(Number(margin[1]) >= 64, `scroll-margin-top ${margin[1]}px is smaller than the header`);
});

test("the phone nav no longer hides links with nth-child arithmetic", () => {
  assert.ok(
    !css.includes(".home-nav-link:not(:last-child)"),
    "the old rule silently hid the wrong link as soon as one was added",
  );
  assert.ok(!css.includes(".home-nav-link:nth-last-child("), "same fragile pattern");
});

test("the overview never opens a Gateway stream before probing one exists", () => {
  assert.doesNotMatch(
    homePage,
    /useGrpcWeb\(\{\s*enabled:\s*true/,
    "the showcase may be static; gate the hook on the probe verdict",
  );
  assert.match(homePage, /enabled:\s*probe === "reachable"/);
  assert.match(homePage, /from "@\/lib\/homeRuntime"/);
  assert.match(homePage, /probeGateway\(/, "the probe is the only way to enable live traffic");
  assert.match(homePage, /shouldStopPolling\(/);
  assert.match(homePage, /nextPollDelay\(/);
});

test("every nav destination exists in the markup", () => {
  const ids = [...homePage.matchAll(/\{\s*id:\s*"(home-[a-z-]+)",\s*label:/g)].map((match) => match[1]);
  assert.ok(ids.length >= 4, `expected the nav section table, found ${ids.length} entries`);
  for (const id of ids) {
    assert.ok(homePage.includes(`id="${id}"`), `nav points at #${id} but nothing carries that id`);
  }
  assert.match(homePage, /aria-current=/, "the scroll-spy must expose the active section");
});

test("the skip link and its target exist", () => {
  assert.match(homePage, /className="home-skip-link"/);
  assert.match(homePage, /href="#home-content"/);
  assert.match(homePage, /id="home-content"/);
});
