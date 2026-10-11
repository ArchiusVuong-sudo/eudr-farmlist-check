/* Farm List Check: small page scripts (maps with Leaflet, busy buttons). No build step. */
"use strict";

const STATUS_COLORS = { ready: "#15803d", repaired: "#2563eb", review: "#b45309", needs_fix: "#c2261d" };
const STATUS_TEXT = { ready: "Ready", repaired: "Repaired, confirm with supplier", review: "Review", needs_fix: "Needs supplier fix" };
const LOSS = "#7c3aed";
const PROTECTED = "#0e7490";
const FOREST = "#4d7c0f";

function busy(form) {
  const btn = form.querySelector("button[type=submit]");
  if (btn) { btn.classList.add("busy"); setTimeout(() => { btn.disabled = true; }, 0); }
  return true;
}

function pickCard(el) {
  document.querySelectorAll(".card").forEach((c) => c.classList.toggle("active", c === el));
}

function baseMap(el) {
  const map = L.map(el, { preferCanvas: true, zoomControl: true, attributionControl: true });
  map.attributionControl.setPrefix('<a href="https://leafletjs.com">Leaflet</a>');
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(map);
  return map;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function initRunMap(el) {
  let data;
  try {
    const res = await fetch(el.dataset.src);
    if (!res.ok) throw new Error(res.statusText);
    data = await res.json();
  } catch (err) {
    el.innerHTML = '<div class="empty"><b>Map data could not be loaded</b>' + escapeHtml(err.message) + "</div>";
    return;
  }
  const map = baseMap(el);
  const plotUrl = el.dataset.plotUrl.replace(/\/0$/, "/");
  const layers = data.layers || {};
  const overlays = {};
  if (layers.country) {
    L.geoJSON(layers.country, { style: { color: "#13261c", weight: 1.5, fill: false, dashArray: "4 3" }, interactive: false }).addTo(map);
  }
  if (layers.forest) {
    overlays["Forest on 31 Dec 2020 (synthetic)"] = L.geoJSON(layers.forest, {
      style: { color: FOREST, weight: 0.5, fillColor: FOREST, fillOpacity: 0.12 }, interactive: false,
    });
  }
  if (layers.loss) {
    overlays["Forest loss after 31 Dec 2020 (synthetic)"] = L.geoJSON(layers.loss, {
      style: { color: LOSS, weight: 0.6, fillColor: LOSS, fillOpacity: 0.55 },
      onEachFeature: (f, l) => l.bindTooltip(`${f.properties.id} · ${f.properties.date} · conf ${f.properties.confidence}`),
    });
  }
  if (layers.protected) {
    overlays["Protected areas (synthetic)"] = L.geoJSON(layers.protected, {
      style: { color: PROTECTED, weight: 1.5, fillColor: PROTECTED, fillOpacity: 0.12, dashArray: "5 4" },
      onEachFeature: (f, l) => l.bindTooltip(f.properties.name),
    });
  }
  const popup = (p) => `<b class="mono">${escapeHtml(p.code)}</b><br>${STATUS_TEXT[p.status]}<br>row ${p.row} · ${p.kind === "polygon" ? "boundary" : "point, assessed as a circle"}<br><a href="${plotUrl}${p.id}">Open the trace →</a>`;
  // true shapes (boundaries and assessed circles) show when zoomed in; a marker per plot keeps them visible when zoomed out
  const shapes = L.geoJSON(data.plots, {
    style: (f) => ({ color: STATUS_COLORS[f.properties.status], weight: 2, fillColor: STATUS_COLORS[f.properties.status], fillOpacity: 0.3 }),
    onEachFeature: (f, l) => l.bindPopup(popup(f.properties)),
  });
  const markers = L.layerGroup();
  shapes.eachLayer((l) => {
    const p = l.feature.properties;
    L.circleMarker(l.getBounds().getCenter(), { radius: 6, weight: 1.5, color: "#fff", fillColor: STATUS_COLORS[p.status], fillOpacity: 1 })
      .bindPopup(popup(p)).bindTooltip(p.code, { direction: "top" }).addTo(markers);
  });
  const plots = L.featureGroup([shapes, markers]).addTo(map);
  overlays["Plots"] = plots;
  L.control.layers(null, overlays, { collapsed: false, position: "topright" }).addTo(map);
  L.control.scale({ imperial: false }).addTo(map);
  if (data.extent && data.extent.xmin !== null) {
    map.fitBounds([[data.extent.ymin, data.extent.xmin], [data.extent.ymax, data.extent.xmax]], { padding: [24, 24] });
  } else if (plots.getLayers().length) {
    map.fitBounds(plots.getBounds(), { padding: [24, 24] });
  } else {
    map.setView([0, 0], 2);
  }
}

function initMiniMap(el) {
  const data = JSON.parse(document.getElementById("mini-data").textContent);
  const map = baseMap(el);
  const color = STATUS_COLORS[data.status] || "#333";
  const group = L.featureGroup().addTo(map);
  (data.hits || []).forEach((h) => {
    if (!h.geom) return;
    const c = h.layer === "forest_loss" ? LOSS : PROTECTED;
    L.geoJSON(h.geom, { style: { color: c, weight: 1.5, fillColor: c, fillOpacity: 0.35 } })
      .bindTooltip(`${h.id}: overlap ${h.overlap_ha.toFixed(3)} ha`, { permanent: h.layer === "forest_loss", direction: "top" })
      .addTo(group);
  });
  const plot = L.geoJSON(data.plot, { style: { color, weight: 2.5, fillColor: color, fillOpacity: 0.2 } }).addTo(group);
  if (data.kind === "point" && data.given) {
    L.geoJSON(data.given, { pointToLayer: (f, ll) => L.circleMarker(ll, { radius: 4, color: "#13261c", weight: 2, fillOpacity: 1 }) }).addTo(group);
  }
  L.control.scale({ imperial: false }).addTo(map);
  const b = plot.getBounds();
  map.fitBounds(b.pad(1.2), { maxZoom: 17 });
}

document.addEventListener("DOMContentLoaded", () => {
  const run = document.getElementById("run-map");
  if (run && window.L) initRunMap(run);
  const mini = document.getElementById("mini-map");
  if (mini && window.L) initMiniMap(mini);
});
