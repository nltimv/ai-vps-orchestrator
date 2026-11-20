// Fails if user-facing source text contains ML jargon. The dashboard must stay jargon-free.
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const FORBIDDEN = /\b(CFG|samplers?|schedulers?|denois\w*|LoRA|checkpoints?|inference|latents?|VAE|fp8|bf16|quantiz\w*|diffusion)\b/i;
const roots = ["src/pages", "src/components"];
const errors = [];

function walk(dir) {
  for (const f of readdirSync(dir)) {
    const p = join(dir, f);
    if (statSync(p).isDirectory()) walk(p);
    else if (p.endsWith(".tsx")) check(p);
  }
}

function check(file) {
  const src = readFileSync(file, "utf8");
  // JSX text between tags and string literals in attributes/props likely to be shown
  const texts = [...src.matchAll(/>([^<>{}]+)</g), ...src.matchAll(/"([^"\n]{3,})"/g)].map((m) => m[1]);
  for (const t of texts) {
    const m = t.match(FORBIDDEN);
    if (m && !/^[a-z0-9_./-]+$/i.test(t.trim())) errors.push(`${file}: "${t.trim().slice(0, 80)}" contains "${m[0]}"`);
  }
}

roots.forEach(walk);
if (errors.length) {
  console.error("Jargon found in user-facing text:\n" + errors.join("\n"));
  process.exit(1);
}
console.log("jargon lint: OK");
