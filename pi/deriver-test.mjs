// Runs the SHARED title-derivation contract (bin/title-vectors.json) against
// the TypeScript extension's deriver — the same vectors the Python host
// suite runs against bin/tendril_titles.py. Invoked by tests/test_titles.py
// (skipped when node is absent) and usable directly:
//   node pi/deriver-test.mjs
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { deriveTitle, deriveSummary, humanizeRole } from "./tendril-session-title.ts";

const here = path.dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(
	fs.readFileSync(path.join(here, "..", "bin", "title-vectors.json"), "utf8"));

let failures = 0;
let total = 0;
for (const v of fixture.derive) {
	total += 1;
	const got = deriveTitle(v.input);
	if (got !== v.expect) {
		failures += 1;
		console.log(`FAIL ${v.id}: got=${JSON.stringify(got)} want=${JSON.stringify(v.expect)}`);
	}
}
for (const v of fixture.summary) {
	total += 1;
	const got = deriveSummary(v.input);
	if (got !== v.expect) {
		failures += 1;
		console.log(`FAIL ${v.id}: got=${JSON.stringify(got)} want=${JSON.stringify(v.expect)}`);
	}
}
// a few role-humanizer pins (host parity)
const roles = [
	["researcher_a1b2c3d4", "Researcher"],
	["worker", "Worker"],
	["Explore", "Explore"],
	["code_reviewer_9e09eff5", "Code reviewer"],
	["", null],
	[null, null],
];
for (const [input, expect] of roles) {
	total += 1;
	const got = humanizeRole(input);
	if (got !== expect) {
		failures += 1;
		console.log(`FAIL role ${JSON.stringify(input)}: got=${JSON.stringify(got)} want=${JSON.stringify(expect)}`);
	}
}
if (failures > 0) {
	console.log(`${failures} failure(s) of ${total}`);
	process.exit(1);
}
console.log(`ok: ${total} vectors pass (typescript deriver)`);
