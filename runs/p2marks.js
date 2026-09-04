const out = [];
for (const name of ['sch_PrimitiveNetPort','sch_PrimitiveNetFlag','sch_PrimitiveLabel']) {
  try {
    const all = await eda[name].getAll();
    for (const m of all) {
      const x = m.getState_X ? Math.round(m.getState_X()) : null;
      const y = m.getState_Y ? Math.round(m.getState_Y()) : null;
      const net = m.getState_Net ? m.getState_Net() : (m.getState_Content ? m.getState_Content() : '');
      const rot = m.getState_Rotation ? m.getState_Rotation() : null;
      out.push({t: name.replace('sch_Primitive',''), x, y, net, rot});
    }
  } catch(e) { out.push({t: name, err: String(e).slice(0,60)}); }
}
return out;
