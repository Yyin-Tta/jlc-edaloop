const wires = await eda.sch_PrimitiveWire.getAll();
const out = {wires: [], texts: [], comps: []};
for (const w of wires) {
  const line = w.getState_Line ? w.getState_Line() : null;
  const net = w.getState_Net ? w.getState_Net() : null;
  if (line) out.wires.push({line: line.map(v=>Math.round(v)), net: net});
}
const texts = await eda.sch_PrimitiveText.getAll();
for (const t of texts) {
  const c = t.getState_Content ? t.getState_Content() : '';
  const x = t.getState_X ? t.getState_X() : 0;
  const y = t.getState_Y ? t.getState_Y() : 0;
  out.texts.push({x: Math.round(x), y: Math.round(y), c: c});
}
const comps = await eda.sch_PrimitiveComponent.getAll();
for (const c of comps) {
  const d = c.getState_Designator ? c.getState_Designator() : '';
  if (['LED12','R21','ULNST3','ULNST4'].includes(d)) {
    const x = c.getState_X ? c.getState_X() : 0;
    const y = c.getState_Y ? c.getState_Y() : 0;
    const rot = c.getState_Rotation ? c.getState_Rotation() : null;
    const pins = [];
    try {
      const ps = await c.getPins ? await c.getPins() : [];
      for (const p of ps) {
        pins.push({n: p.getState_Number ? p.getState_Number() : '', x: Math.round(p.getState_X ? p.getState_X() : 0), y: Math.round(p.getState_Y ? p.getState_Y() : 0)});
      }
    } catch(e) { pins.push({err: String(e)}); }
    out.comps.push({d, x: Math.round(x), y: Math.round(y), rot, pins});
  }
}
return out;
