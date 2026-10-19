// decimate.mjs — shrink a TRELLIS GLB to game weight. TRELLIS emits ~500k triangles per object
// (~16MB, 99% geometry; textures are already webp) — far past what a low-poly village needs.
// Weld + meshopt-simplify toward a triangle budget, then quantize attributes
// (KHR_mesh_quantization — three's GLTFLoader decodes it natively, no decoder wasm needed).
// Foliage-like meshes (thousands of disconnected leaf shells) resist topological collapse — for
// those, fall back to meshopt's SLOPPY simplifier, which ignores topology and hits the budget.
//   node decimate.mjs <in.glb> <out.glb> [maxTris=20000]
import { NodeIO, getBounds } from "@gltf-transform/core";
import { ALL_EXTENSIONS } from "@gltf-transform/extensions";
import { weld, simplify, quantize, prune, dedup, textureCompress } from "@gltf-transform/functions";
import { MeshoptSimplifier } from "meshoptimizer";
import sharp from "sharp";

const [, , inPath, outPath, maxTrisArg] = process.argv;
const maxTris = Number(maxTrisArg) || 20000;

const io = new NodeIO().registerExtensions(ALL_EXTENSIONS);
const doc = await io.read(inPath);

const countTris = () => {
  let tris = 0;
  for (const mesh of doc.getRoot().listMeshes())
    for (const prim of mesh.listPrimitives()) {
      const idx = prim.getIndices();
      tris += (idx ? idx.getCount() : prim.getAttribute("POSITION").getCount()) / 3;
    }
  return tris;
};

const before = countTris();
const ratio = Math.min(1, maxTris / Math.max(1, before));
// error stays SMALL here — unbounded collapse tears buildings apart. Structured meshes come out a
// little over budget but intact; only foliage-class geometry (thousands of disconnected leaf
// shells, stalls out at ~10x budget) drops to the topology-ignoring pass below.
await doc.transform(dedup(), weld(), simplify({ simplifier: MeshoptSimplifier, ratio, error: 0.05 }));

let sloppy = false;
if (countTris() > maxTris * 5) {
  sloppy = true;
  await MeshoptSimplifier.ready;
  const total = countTris();
  for (const mesh of doc.getRoot().listMeshes()) {
    for (const prim of mesh.listPrimitives()) {
      const idx = prim.getIndices();
      const pos = prim.getAttribute("POSITION");
      if (!idx || !pos) continue;
      const primTris = idx.getCount() / 3;
      const target = Math.min(idx.getCount(), Math.max(96, Math.round(maxTris * primTris / total)) * 3);
      const positions = new Float32Array(pos.getArray());
      const indices = new Uint32Array(idx.getArray());
      const [newIndices] = MeshoptSimplifier.simplifySloppy(indices, positions, 3, null, target, 1);
      idx.setArray(newIndices);
    }
  }
}

// Normalize scale: longest side becomes exactly 1 unit, so a game can scale every mesh to its
// real-world size from one known fact instead of guessing per file.
let span = 0;
for (const scene of doc.getRoot().listScenes()) {
  const b = getBounds(scene);
  span = Math.max(span, ...[0, 1, 2].map((i) => b.max[i] - b.min[i]));
}
if (span > 0 && Math.abs(span - 1) > 1e-3) {
  const s = 1 / span;
  for (const scene of doc.getRoot().listScenes())
    for (const node of scene.listChildren()) {
      const sc = node.getScale();
      node.setScale([sc[0] * s, sc[1] * s, sc[2] * s]);
      const t = node.getTranslation();
      node.setTranslation([t[0] * s, t[1] * s, t[2] * s]);
    }
}

for (const material of doc.getRoot().listMaterials()) material.setDoubleSided(false);

await doc.transform(quantize(), textureCompress({ encoder: sharp, targetFormat: "webp" }), prune());
await io.write(outPath, doc);
console.log(JSON.stringify({ before, after: countTris(), sloppy, span }));
