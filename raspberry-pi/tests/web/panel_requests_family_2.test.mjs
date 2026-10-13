/*
  ANH-190. The other runs of the enumeration of panel_requests_family_1.test.mjs: a frame the
  console did not deliver, a loop that refuses the starts of one screen for a reason of its own,
  and a fourth control cycle.

  The model of the console, the invariant and the way it is held are in request_family.mjs.
*/
import assert from "node:assert/strict";
import test from "node:test";

import { enumerate, summary } from "./request_family.mjs";

test(`every interleaving of up to three requests, with a frame lost, a loop that refuses, or a fourth cycle: each note says "pris", "refus" or "annulee" if and only if that became of its screen's request`, async () => {
  const result = await enumerate([
    // A frame the console coalesced away for a slow socket.
    { counts: [1, 2, 3], manual: [false], run: { dropped: [1] } },
    { counts: [1, 2], manual: [true], run: { dropped: [1] } },
    // The loop refuses the starts of one screen for a reason of its own: a drive in fault.
    { counts: [1, 2, 3], manual: [false], run: { loop: { refusesStartsOf: 1 } } },
    { counts: [1, 2, 3], manual: [false], run: { loop: { refusesStartsOf: 2 } } },
    // One cycle more. An interleaving that fits in three cycles is one of the runs above, moved
    // in time: the ones played here need the four, a request in the first cycle and one in the last.
    {
      counts: [2, 3],
      manual: [false],
      run: { cycles: 4 },
      only: (requests) => requests[0].slot < 3 && requests[requests.length - 1].slot >= 9,
    },
  ]);
  // The invariant is not held by a page that says nothing: each answer is met, many times.
  const { ends } = result;
  assert.ok(Object.entries(ends).every(([what, count]) => what === "sent" || count > 1000), JSON.stringify(ends));
  // And once the last tick has gone by, no request is left without its answer.
  assert.equal(ends.sent, 0);
  console.log(`# start-note family, 2 of 2: ${summary(result)}`);
});
