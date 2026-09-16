import "leaflet/dist/leaflet.css";
import L from "leaflet";
import { useEffect, useMemo, useRef } from "react";

import type { City, Stop } from "@/lib/travel/types";

interface Props {
  city: City;
  stops: Stop[];
  ghostStops?: Stop[];
  activeIndex: number | null;
}

function pinIcon(label: string, variant: "live" | "ghost", active: boolean) {
  const base =
    variant === "live"
      ? "background:var(--color-route);color:var(--color-primary-foreground);border:2px solid rgba(255,255,255,0.9);"
      : "background:var(--color-card);color:var(--color-added);border:2px dashed var(--color-added);";
  return L.divIcon({
    className: "",
    html: `<div style="${base}width:30px;height:30px;border-radius:999px;display:flex;align-items:center;justify-content:center;font:600 13px/1 Inter,sans-serif;box-shadow:0 4px 12px rgba(0,0,0,.22);${
      active ? "transform:scale(1.25);" : ""
    }">${label}</div>`,
    iconSize: [30, 30],
    iconAnchor: [15, 15],
  });
}

export default function MapView({ city, stops, ghostStops = [], activeIndex }: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<L.Map | null>(null);
  const layerRef = useRef<L.LayerGroup | null>(null);

  const placeStops = useMemo(() => stops.filter((s) => s.kind === "place"), [stops]);
  const ghosts = useMemo(
    () =>
      ghostStops.filter(
        (g) => g.kind === "place" && !placeStops.some((s) => s.placeId === g.placeId),
      ),
    [ghostStops, placeStops],
  );

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    const map = L.map(containerRef.current, { zoomControl: true, attributionControl: true }).setView(
      [city.center.lat, city.center.lng],
      13,
    );
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: "&copy; OpenStreetMap contributors",
      maxZoom: 19,
    }).addTo(map);
    layerRef.current = L.layerGroup().addTo(map);
    mapRef.current = map;
    return () => {
      map.remove();
      mapRef.current = null;
      layerRef.current = null;
    };
  }, [city.center.lat, city.center.lng]);

  useEffect(() => {
    const map = mapRef.current;
    const layer = layerRef.current;
    if (!map || !layer) return;
    layer.clearLayers();

    const pts: L.LatLngExpression[] = placeStops.map((s) => [s.lat, s.lng]);
    if (pts.length > 1) {
      L.polyline(pts, {
        color: "var(--color-route)",
        weight: 4,
        opacity: 0.85,
        dashArray: "1 8",
        lineCap: "round",
      }).addTo(layer);
    }

    placeStops.forEach((s, i) => {
      L.marker([s.lat, s.lng], { icon: pinIcon(String(i + 1), "live", activeIndex === i) })
        .bindPopup(`<strong>${s.name}</strong>`)
        .addTo(layer);
    });

    ghosts.forEach((g) => {
      L.marker([g.lat, g.lng], { icon: pinIcon("+", "ghost", false) })
        .bindPopup(`<strong>${g.name}</strong><br/>Proposed`)
        .addTo(layer);
    });

    const all = [...pts, ...ghosts.map((g) => [g.lat, g.lng] as L.LatLngExpression)];
    if (all.length > 0) {
      map.fitBounds(L.latLngBounds(all).pad(0.25), { animate: true });
    } else {
      map.setView([city.center.lat, city.center.lng], 13);
    }
  }, [placeStops, ghosts, activeIndex, city.center.lat, city.center.lng]);

  return <div ref={containerRef} className="h-full w-full" />;
}
