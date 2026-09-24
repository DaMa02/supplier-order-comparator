# Browser tests for real bug sequences

The tests in `tests/` run `app.js` with a stub `addEventListener`: they
check that a button exists with the right `data-action`, not that clicking
it actually works. A visible UI change still needs a manual check in the
browser.

These tests cover that gap, for a specific set of sequences. They aren't,
and shouldn't become, full UI coverage: each one replays a sequence that has
already caused a real bug, and the test comment states which regression it
guards against.

## Running them

```
cd prove-browser
npm install
npx playwright install chromium
npm run prove
```

`npm run prove:vedi` runs them headed, so you can watch.

## Not part of the store deployment

This folder isn't part of the shipped program. The store PC doesn't install
or run it, and doesn't need to: `requirements.txt` stays a single line, and
the writer's Node dependency doesn't change. These tests run here and in CI.

## Why there are few, and why that's deliberate

A browser test that fails on a timing issue instead of a real regression
teaches people to stop looking at red runs. Nine tests that get read are
worth more than ninety that get ignored: a flaky one gets fixed or removed,
not given a longer wait.
