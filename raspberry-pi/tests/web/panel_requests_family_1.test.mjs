/*
  ANH-190. What each screen's note says of its own start request, for every interleaving of up
  to three requests over three control cycles: the orderings the reviews found, then all of them,
  for programme starts and for manual ones, with a frame delivered after the events of the cycle
  that follows it, and with a loop that refuses a reset at rest.

  The model of the console, the invariant and the way it is held are in request_family.mjs. The
  other runs (a frame not delivered, a loop that refuses the starts of one screen, a fourth
  cycle) are in panel_requests_family_2.test.mjs.
*/
import assert from "node:assert/strict";
import test from "node:test";

import {
  CANCELLED,
  DRIVE_FAULT,
  FAMILY_NAMED,
  START_PENDING,
  describeFamily,
  enumerate,
  panel,
  playFamily,
  summary,
  takenBy,
} from "./request_family.mjs";

test("a page played after another starts from nothing", async () => {
  // Given a page that has lived a whole history: a start taken, said so under its button.
  const notes = await playFamily([{ kind: "start", slot: 0, screen: 1 }], true);
  assert.equal(notes.get(1), takenBy(0, true));
  // When the next page is made, in the context the one before ran in.
  const { context, node } = panel();
  // Then nothing of the one before is left: no note, no request, no answer, no frame.
  assert.equal(node("manual-note").textContent, "");
  assert.equal(node("start-note").textContent, "");
  assert.equal(node("manual-bench").checked, undefined);
  assert.deepEqual(
    JSON.parse(JSON.stringify([context.state.asked, context.state.held, context.state.fates, context.state.sessionsOver, context.state.events])),
    [{}, {}, [], [], []],
  );
  assert.equal(context.state.snapshot, null);
  assert.equal(context.state.streamOpened, null);
  assert.equal(context.state.statusSent, 0);
});

test("two starts in one cycle: the second is refused at once, and the first screen is told its start was taken", async () => {
  for (const manual of [true, false]) {
    const notes = await playFamily(FAMILY_NAMED.twoStarts, manual);
    // Before: both got a 202, one request replaced the other in silence, and neither screen could say.
    assert.equal(notes.get(1), takenBy(0, manual));
    assert.equal(notes.get(2), START_PENDING);
  }
});

test("three requests in two cycles: the start asked while the loop armed another is refused at once", async () => {
  for (const manual of [true, false]) {
    const notes = await playFamily(FAMILY_NAMED.threeRequests, manual);
    // Before: screen 2 got a 202, and a refusal a tick later that no screen could tell was its own.
    assert.equal(notes.get(1), takenBy(1, manual));
    assert.equal(notes.get(2), START_PENDING);
  }
});

test("start, stop, start with the stop's frames coalesced: each screen says what became of its own request", async () => {
  for (const manual of [true, false]) {
    const notes = await playFamily(FAMILY_NAMED.startStopStart, manual, { dropped: [1] });
    assert.equal(notes.get(1), "");
    assert.equal(notes.get(2), takenBy(2, manual));
  }
});

test("a start a STOP displaced before the loop took it is said cancelled, and the next start of that screen is said taken", async () => {
  for (const manual of [true, false]) {
    // The first start, cancelled: the screen is told, where it used to be left waiting for ever.
    const displaced = await playFamily(FAMILY_NAMED.startCancelled.slice(0, 2), manual);
    assert.ok(displaced.get(1).startsWith("demande annulee ("), displaced.get(1));
    assert.ok(displaced.get(1).endsWith(") : " + CANCELLED), displaced.get(1));
    // And a later start of the same screen, on the same socket, is not held in doubt by it.
    const notes = await playFamily(FAMILY_NAMED.startCancelled, manual);
    assert.equal(notes.get(1), takenBy(2, manual));
  }
});

test("a start the loop refuses for a reason of its own is said refused on its screen, and on no other", async () => {
  for (const manual of [true, false]) {
    const requests = [{ kind: "start", slot: 0, screen: 1 }, { kind: "start", slot: 4, screen: 2 }];
    const notes = await playFamily(requests, manual, { loop: { refusesStartsOf: 1 } });
    assert.ok(notes.get(1).startsWith("refus de la machine ("), notes.get(1));
    assert.ok(notes.get(1).endsWith(") : " + DRIVE_FAULT), notes.get(1));
    assert.equal(notes.get(2), takenBy(1, manual));
  }
});

test(`every interleaving of up to three requests over three cycles: each note says "pris", "refus" or "annulee" if and only if that became of its screen's request`, async () => {
  const result = await enumerate([
    // Programme starts, whose words are their own, and manual starts, three requests as well.
    { counts: [1, 2, 3], manual: [false, true], run: {} },
    // A frame overtaken by the events of the cycle that follows it.
    { counts: [1, 2, 3], manual: [false], run: { late: [1] } },
    { counts: [1, 2], manual: [true], run: { late: [1] } },
    // A loop that refuses a reset at rest: no fault to reset.
    { counts: [1, 2, 3], manual: [false], run: { loop: { refusesResets: true } } },
  ]);
  // The orderings the reviews found are among them.
  for (const [name, requests] of Object.entries(FAMILY_NAMED)) {
    assert.ok(result.seen.has(describeFamily(requests)), name);
  }
  // The invariant is not held by a page that says nothing: each answer is met, many times. No
  // start is refused by the loop in these runs: that is the other file.
  const { ends } = result;
  assert.ok(ends.taken > 1000 && ends.cancelled > 1000 && ends["at once"] > 1000 && ends.nothing > 1000, JSON.stringify(ends));
  assert.equal(ends.refused, 0);
  // And once the last tick has gone by, no request is left without its answer.
  assert.equal(ends.sent, 0);
  console.log(`# start-note family, 1 of 2: ${summary(result)}`);
});
