#!/usr/bin/env python3
"""
twstft_app.py
=============
Servidor FastAPI para el visor de sesiones TWSTFT.

Sirve:
  GET /              → HTML de la app React (todo en uno)
  GET /api/ficheros  → lista de ficheros ITU disponibles
  GET /api/dia/{mjd} → datos del día (todos los registros del fichero)

Arranque:
    python3 twstft_app.py [--config twstft.ini] [--itu-dir /ruta] [--port 8050]

Para producción:
    uvicorn twstft_app:app --host 0.0.0.0 --port 8050 --workers 2
"""

import argparse
import configparser
import glob
import os
import re
from typing import Optional
from dataclasses import dataclass, asdict

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

CONFIG_DEFAULT  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "twstft.ini")
ITU_DIR_DEFAULT = "/var/log/satres/itu"

SW_DESC = {
    "0": "S=0 · dif. estación terrena",
    "1": "S=1 · calibrado GPS/enlace",
    "2": "S=2 · medida de distancia",
    "5": "S=5 · datos combinados",
    "6": "S=6 · red",
    "9": "S=9 · sin calibrar",
}

# ---------------------------------------------------------------------------
# Modelos
# ---------------------------------------------------------------------------

@dataclass
class Registro:
    loc:      str
    rem:      str
    link:     int
    mjd:      int
    sttime:   str
    ntl:      int
    tw:       float
    drms:     float
    smp:      int
    atl:      int
    refdelay: float
    rsig:     float
    ci:       str
    sw:       int
    calr:     float
    esdvar:   float
    esig:     float
    tmp:      Optional[int]
    hum:      Optional[int]
    pres:     Optional[int]

    @property
    def session_h(self) -> int:
        hh = int(self.sttime[:2])
        return hh - (hh % 2)

    @property
    def calr_valido(self) -> bool:
        return abs(self.calr) < 999_999_990

    @property
    def esdvar_valido(self) -> bool:
        return abs(self.esdvar) < 999_999_990

    @property
    def esig_valido(self) -> bool:
        return self.esig < 99990

    def to_dict(self) -> dict:
        d = asdict(self)
        d["session_h"]     = self.session_h
        d["calr_valido"]   = self.calr_valido
        d["esdvar_valido"] = self.esdvar_valido
        d["esig_valido"]   = self.esig_valido
        d["sw_desc"]       = SW_DESC.get(str(self.sw), f"S={self.sw}")
        d["tw_ns"]         = self.tw * 1e9
        return d


# ---------------------------------------------------------------------------
# Parser ITU
# ---------------------------------------------------------------------------

def parsear_fichero_itu(ruta: str) -> list[Registro]:
    registros = []
    with open(ruta, encoding="utf-8", errors="replace") as f:
        for linea in f:
            if linea.startswith("*") or not linea.strip():
                continue
            p = linea.split()
            if len(p) < 18:
                continue
            try:
                registros.append(Registro(
                    loc      = p[0],
                    rem      = p[1],
                    link     = int(p[2]),
                    mjd      = int(p[3]),
                    sttime   = p[4],
                    ntl      = int(p[5]),
                    tw       = float(p[6]),
                    drms     = float(p[7]),
                    smp      = int(p[8]),
                    atl      = int(p[9]),
                    refdelay = float(p[10]),
                    rsig     = float(p[11]),
                    ci       = p[12],
                    sw       = int(p[13]),
                    calr     = float(p[14]),
                    esdvar   = float(p[15]),
                    esig     = float(p[16]),
                    tmp      = int(p[17]) if len(p) > 17 else None,
                    hum      = int(p[18]) if len(p) > 18 else None,
                    pres     = int(p[19]) if len(p) > 19 else None,
                ))
            except (ValueError, IndexError):
                continue
    return registros


# ---------------------------------------------------------------------------
# Estado global del servidor
# ---------------------------------------------------------------------------

itu_dir_global  = ITU_DIR_DEFAULT
labs_orden_ini: list[str] = []
cache_registros: dict[str, list[Registro]] = {}


def cargar_orden_labs(config_path: str) -> list[str]:
    if not os.path.isfile(config_path):
        return []
    cfg = configparser.ConfigParser(
        interpolation=None, inline_comment_prefixes=("#",)
    )
    cfg.read(config_path, encoding="utf-8")
    labs = []
    for sec in cfg.sections():
        if not sec.lower().startswith("lab "):
            continue
        try:
            nombre = cfg.get(sec, "nombre").strip()
            minuto = cfg.getint(sec, "minuto")
            labs.append((minuto, nombre))
        except (configparser.NoOptionError, ValueError):
            continue
    labs.sort(key=lambda x: x[0])
    return [n for _, n in labs]


def listar_ficheros() -> list[dict]:
    patron = os.path.join(itu_dir_global, "tw*.*")
    ficheros = [f for f in glob.glob(patron)
                if not f.endswith((".py", ".ini", ".log"))]
    ficheros.sort(reverse=True)
    resultado = []
    for ruta in ficheros:
        nombre = os.path.basename(ruta)
        m = re.search(r'(\d{2})\.?(\d{3})', nombre)
        mjd = int(m.group(1) + m.group(2)) if m else None
        resultado.append({"nombre": nombre, "ruta": ruta, "mjd": mjd})
    return resultado


def obtener_registros(ruta: str) -> list[Registro]:
    # Invalidar caché si el fichero fue modificado
    mtime = os.path.getmtime(ruta)
    clave = f"{ruta}:{mtime}"
    if clave not in cache_registros:
        cache_registros.clear()  # evitar crecimiento ilimitado
        cache_registros[clave] = parsear_fichero_itu(ruta)
    return cache_registros[clave]


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------

app = FastAPI(title="TWSTFT Viewer")


@app.get("/api/ficheros")
def api_ficheros():
    return listar_ficheros()


@app.get("/api/dia/{nombre_fichero}")
def api_dia(nombre_fichero: str):
    # Buscar el fichero por nombre en el directorio ITU
    ruta = os.path.join(itu_dir_global, nombre_fichero)
    if not os.path.isfile(ruta):
        raise HTTPException(status_code=404, detail="Fichero no encontrado")

    registros = obtener_registros(ruta)

    # Ordenar labs según ini; los que no estén van al final
    todos_labs = list({r.rem for r in registros})
    if labs_orden_ini:
        labs_ord = [l for l in labs_orden_ini if l in todos_labs]
        labs_ord += sorted(set(todos_labs) - set(labs_ord))
    else:
        labs_ord = sorted(todos_labs)

    # Sesiones presentes
    sesiones = sorted({r.session_h for r in registros})

    # Pivot: lab → session_h → mejor registro (menor DRMS)
    from collections import defaultdict
    pivot: dict[str, dict[int, Registro]] = defaultdict(dict)
    for r in registros:
        sh = r.session_h
        if sh not in pivot[r.rem] or r.drms < pivot[r.rem][sh].drms:
            pivot[r.rem][sh] = r

    # Estadísticas del día
    drms_vals = [r.drms for r in registros]
    stats = {
        "mjd":       registros[0].mjd if registros else None,
        "n_labs":    len(todos_labs),
        "n_sesiones": len(sesiones),
        "n_registros": len(registros),
        "drms_medio": round(sum(drms_vals) / len(drms_vals), 3) if drms_vals else 0,
        "drms_max":   round(max(drms_vals), 3) if drms_vals else 0,
        "drms_min":   round(min(drms_vals), 3) if drms_vals else 0,
    }

    # Construir tabla serializable
    tabla = []
    for lab in labs_ord:
        fila = {"lab": lab, "sesiones": {}}
        for sh in sesiones:
            r = pivot[lab].get(sh)
            fila["sesiones"][str(sh)] = r.to_dict() if r else None
        tabla.append(fila)

    return {
        "stats":    stats,
        "sesiones": sesiones,
        "tabla":    tabla,
    }


# ---------------------------------------------------------------------------
# HTML de la app React (todo en uno)
# ---------------------------------------------------------------------------



HTML_APP = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TWSTFT · ROA</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:       #f2f0e8;
    --bg2:      #ffffff;
    --bg3:      #eae8e0;
    --border:   rgba(0,0,0,.10);
    --border2:  rgba(0,0,0,.06);
    --text:     #1a1a1a;
    --text2:    #666560;
    --text3:    #aaa99f;
    --accent:   #185FA5;
    --font-h:   'IBM Plex Sans', sans-serif;
    --font-m:   'IBM Plex Mono', monospace;
    --radius:   10px;
    --radius-lg:14px;
  }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--font-m);
    font-size: 13px;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }
  ::-webkit-scrollbar { width: 5px; height: 5px; }
  ::-webkit-scrollbar-track { background: var(--bg); }
  ::-webkit-scrollbar-thumb { background: rgba(0,0,0,.15); border-radius: 3px; }
  ::-webkit-scrollbar-thumb:hover { background: rgba(0,0,0,.25); }

  /* ── Layout ── */
  .app { display: flex; flex-direction: column; height: 100vh; overflow: hidden; }

  /* ── Topbar ── */
  .topbar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 0 20px; height: 52px; flex-shrink: 0;
    background: var(--bg2);
    border-bottom: 0.5px solid var(--border);
  }
  .brand { display: flex; align-items: baseline; gap: 8px; }
  .brand-title {
    font-family: var(--font-h); font-size: 16px; font-weight: 700;
    letter-spacing: -.01em; color: var(--text);
  }
  .brand-sub { font-size: 11px; color: var(--text3); }

  /* ── Selector ── */
  .file-select {
    font-family: var(--font-m); font-size: 12px;
    padding: 5px 10px; border-radius: var(--radius);
    border: 0.5px solid var(--border); background: var(--bg3);
    color: var(--text2); outline: none; cursor: pointer;
    min-width: 240px; transition: border-color .15s;
  }
  .file-select:hover, .file-select:focus { border-color: var(--accent); }

  /* ── Stats bar ── */
  .statsbar {
    display: flex; align-items: center; gap: 0;
    padding: 0 20px; height: 48px; flex-shrink: 0;
    background: var(--bg2); border-bottom: 0.5px solid var(--border2);
  }
  .stat {
    display: flex; align-items: baseline; gap: 6px;
    padding: 0 16px; border-right: 0.5px solid var(--border2);
    font-size: 11px; color: var(--text3);
  }
  .stat:first-child { padding-left: 0; }
  .stat-val {
    font-size: 14px; font-weight: 600; color: var(--text);
    font-family: var(--font-m); font-variant-numeric: tabular-nums;
  }

  /* ── Tabla ── */
  .table-wrap {
    flex: 1; overflow: auto;
    padding: 16px 20px 0;
  }
  .table-container {
    border: 0.5px solid var(--border);
    border-radius: var(--radius-lg);
    overflow: hidden;
    background: var(--bg2);
  }
  table { border-collapse: collapse; width: max-content; min-width: 100%; }

  thead th {
    background: var(--bg3);
    padding: 8px 6px;
    font-family: var(--font-m); font-size: 11px; font-weight: 500;
    letter-spacing: .04em; text-transform: uppercase; color: var(--text3);
    border-bottom: 0.5px solid var(--border);
    text-align: center; white-space: nowrap; min-width: 82px; max-width: 110px;
  }
  thead th.th-lab {
    text-align: left; padding-left: 16px;
    min-width: 118px; width: 118px;
    position: sticky; left: 0; z-index: 3;
    background: var(--bg3);
  }
  tbody tr { transition: background .08s; }
  tbody tr:nth-child(even) td.td-lab { background: #fafaf7; }
  tbody tr:hover td.td-lab { background: #eef4fb !important; }
  tbody tr:hover td.td-empty { background: #eef4fb !important; }

  td.td-lab {
    background: var(--bg2);
    padding: 0 14px 0 16px; height: 52px;
    white-space: nowrap;
    border-bottom: 0.5px solid var(--border2);
    border-right: 0.5px solid var(--border);
    position: sticky; left: 0; z-index: 1;
    transition: background .08s;
  }
  .lab-name {
    font-family: var(--font-m); font-size: 13px; font-weight: 600;
    color: var(--text); letter-spacing: 0;
  }
  .lab-ncal {
    font-size: 9px; font-family: var(--font-m);
    color: #854F0B; background: #FAEEDA;
    padding: 1px 5px; border-radius: 4px;
    margin-left: 6px; font-weight: 500;
  }

  td.td-cell {
    height: 52px; padding: 0;
    border-bottom: 0.5px solid rgba(0,0,0,.09);
    border-right: 0.5px solid rgba(0,0,0,.06);
    cursor: pointer;
    transition: filter .1s, transform .1s;
  }
  td.td-cell:hover {
    filter: brightness(1.1);
    transform: scaleY(1.05);
    position: relative; z-index: 1;
  }
  td.td-empty {
    background: var(--bg2); height: 52px;
    border-bottom: 0.5px solid var(--border2);
    border-right: 0.5px solid var(--border2);
    text-align: center; color: var(--text3);
    font-size: 16px; vertical-align: middle;
    transition: background .08s;
  }
  .cell-inner {
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    height: 100%; gap: 1px; padding: 4px 6px;
  }
  .cell-drms {
    font-family: var(--font-m); font-size: 14px; font-weight: 600;
    line-height: 1; letter-spacing: 0;
    font-variant-numeric: tabular-nums;
  }
  .cell-calr { font-size: 9px; opacity: .72; font-variant-numeric: tabular-nums; }
  .cell-smp  { font-size: 9px; opacity: .52; font-variant-numeric: tabular-nums; }

  /* ── Leyenda ── */
  .leyenda {
    display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
    padding: 10px 20px 12px; flex-shrink: 0;
    font-size: 11px; color: var(--text3);
  }
  .leg-item { display: flex; align-items: center; gap: 5px; }
  .leg-swatch { width: 12px; height: 12px; border-radius: 3px; flex-shrink: 0; }

  /* ── Modal overlay ── */
  .modal-overlay {
    position: fixed; inset: 0; z-index: 50;
    background: rgba(0,0,0,.35);
    backdrop-filter: blur(3px);
    display: flex; align-items: center; justify-content: center;
    animation: fadein .15s ease;
  }
  @keyframes fadein { from { opacity: 0; } to { opacity: 1; } }

  /* ── Modal ── */
  .modal {
    width: min(580px, 95vw);
    background: var(--bg2);
    border: 0.5px solid var(--border);
    border-radius: var(--radius-lg);
    overflow: hidden;
    box-shadow: 0 20px 60px rgba(0,0,0,.18);
    animation: slideup .18s cubic-bezier(.16,1,.3,1);
    max-height: 92vh; display: flex; flex-direction: column;
    position: relative;
  }
  @keyframes slideup {
    from { opacity: 0; transform: translateY(14px) scale(.98); }
    to   { opacity: 1; transform: none; }
  }
  .modal-hdr {
    padding: 18px 20px 16px;
    display: flex; justify-content: space-between; align-items: flex-start;
    flex-shrink: 0;
  }
  .modal-lab {
    font-family: var(--font-m); font-size: 22px; font-weight: 600;
    letter-spacing: 0; line-height: 1;
  }
  .modal-meta { font-size: 11px; margin-top: 4px; opacity: .7; }
  .modal-drms-val {
    font-family: var(--font-m); font-size: 32px; font-weight: 600;
    line-height: 1; letter-spacing: 0;
    font-variant-numeric: tabular-nums;
  }
  .modal-drms-lbl { font-size: 10px; opacity: .65; margin-top: 2px; text-align: right; }
  .modal-close {
    position: absolute; top: 14px; right: 14px;
    background: rgba(0,0,0,.08); border: none;
    font-size: 14px; width: 26px; height: 26px;
    border-radius: 6px; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    transition: background .12s; color: inherit;
  }
  .modal-close:hover { background: rgba(0,0,0,.16); }
  .modal-body { overflow-y: auto; padding: 0 20px 18px; flex: 1; }
  .modal-sec {
    font-size: 9px; font-weight: 600; letter-spacing: .08em;
    text-transform: uppercase; color: var(--text3);
    padding: 10px 0 6px;
    border-top: 0.5px solid var(--border2); margin-bottom: 7px;
  }
  .kv-grid   { display: grid; grid-template-columns: repeat(3,1fr); gap: 6px; }
  .kv-grid-2 { grid-template-columns: repeat(2,1fr); }
  .kv-grid-4 { grid-template-columns: repeat(4,1fr); }
  .kv {
    background: var(--bg3); border-radius: var(--radius);
    padding: 7px 10px;
    border: 0.5px solid var(--border2);
  }
  .kv-label {
    font-size: 9px; color: var(--text3); text-transform: uppercase;
    letter-spacing: .05em; margin-bottom: 3px;
  }
  .kv-val {
    font-size: 13px; font-weight: 500; color: var(--text);
    font-variant-numeric: tabular-nums;
  }
  .kv-val.big { font-size: 16px; font-family: var(--font-m); font-weight: 600; }
  .kv-val.muted { color: var(--text3); font-weight: 400; font-style: italic; }
  .kv-val.ok   { color: #3B6D11; }
  .kv-val.warn { color: #854F0B; }

  /* ── Loading / empty ── */
  .loading {
    display: flex; align-items: center; justify-content: center;
    height: 200px; color: var(--text3); gap: 10px;
  }
  .spinner {
    width: 16px; height: 16px;
    border: 2px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin .75s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .empty { padding: 60px; text-align: center; color: var(--text3); }
</style>
</head>
<body>
<div id="app"></div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/react/18.2.0/umd/react.production.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/react-dom/18.2.0/umd/react-dom.production.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/babel-standalone/7.23.2/babel.min.js"></script>

<script type="text/babel">
const { useState, useEffect, useCallback } = React;

// ── Color DRMS ──────────────────────────────────────────────────────────────

const DRMS_V = 0.35, DRMS_A = 0.65, DRMS_R = 1.00;

function drmsColor(d) {
  let h, s, l;
  if (d <= DRMS_V) {
    const t = d / DRMS_V;
    h = 120 - Math.round(t * 20); s = 52; l = 36 - Math.round(t * 3);
  } else if (d <= DRMS_A) {
    const t = (d - DRMS_V) / (DRMS_A - DRMS_V);
    h = 100 - Math.round(t * 65); s = 58; l = 38 + Math.round(t * 4);
  } else if (d <= DRMS_R) {
    const t = (d - DRMS_A) / (DRMS_R - DRMS_A);
    h = 35 - Math.round(t * 35); s = 62; l = 42 - Math.round(t * 5);
  } else {
    h = 0; s = 60; l = 36;
  }
  return `hsl(${h},${s}%,${l}%)`;
}

function drmsTextColor(d) {
  return d <= DRMS_A ? '#0f1f04' : '#ffffff';
}

function drmsLabel(d) {
  if (d <= DRMS_V)    return 'Bajo';
  if (d <= DRMS_A)    return 'Medio';
  if (d <= DRMS_R)    return 'Alto';
  return 'Muy alto';
}



// ── Sesiones del día ─────────────────────────────────────────────────────────

const SESIONES = [0,2,4,6,8,10,12,14,16,18,20,22];

// ── Componente: celda de la tabla ────────────────────────────────────────────

function Celda({ r, ncal, onClick }) {
  if (!r) return <td className="td-empty">·</td>;

  const bg  = drmsColor(r.drms);
  const fg  = drmsTextColor(r.drms);
  const calr = r.calr_valido ? `${r.calr.toFixed(2)} ns` : '—';

  return (
    <td className="td-cell" style={{ background: bg, color: fg }}
        onClick={() => onClick(r)}>
      <div className="cell-inner">
        <span className="cell-drms">{r.drms.toFixed(3)}</span>
        <span className="cell-calr">{calr}</span>
        <span className="cell-smp">{r.smp}/{r.ntl}</span>
      </div>
    </td>
  );
}

// ── Componente: modal de detalle ─────────────────────────────────────────────

function Modal({ r, onClose }) {
  const bg  = drmsColor(r.drms);
  const fg  = drmsTextColor(r.drms);
  const hora = r.sttime.replace(/^(\d{2})(\d{2})(\d{2})$/, '$1:$2:$3');

  const KV = ({ label, val, cls = '' }) => (
    <div className="kv">
      <div className="kv-label">{label}</div>
      <div className={`kv-val ${cls}`}>{val ?? '—'}</div>
    </div>
  );

  return (
    <div className="modal-overlay" onClick={e => e.target === e.currentTarget && onClose()}>
      <div className="modal">
        <div className="modal-hdr" style={{ background: bg, color: fg }}>
          <div>
            <div className="modal-lab">{r.rem}</div>
            <div className="modal-meta">
              vs {r.loc} · Link {r.link} · {hora} UTC · MJD {r.mjd}
            </div>
          </div>
          <div style={{ textAlign: 'right', marginRight: 32 }}>
            <div className="modal-drms-val">{r.drms.toFixed(3)}</div>
            <div className="modal-drms-lbl">ns DRMS · {drmsLabel(r.drms)}</div>
          </div>
        </div>
        <button className="modal-close" onClick={onClose}>✕</button>

        <div className="modal-body">
          <div className="modal-sec">Resultado de la medida</div>
          <div className="kv-grid kv-grid-2" style={{ marginBottom: 6 }}>
            <KV label="TW [ns]"   val={r.tw_ns?.toFixed(3)}    cls="big" />
            <KV label="DRMS [ns]" val={r.drms.toFixed(3)}      cls="big" />
          </div>
          <div className="kv-grid kv-grid-4">
            <KV label="Muestras"  val={`${r.smp} / ${r.ntl}`} />
            <KV label="ATL [s]"   val={r.atl} />
            <KV label="NTL [s]"   val={r.ntl} />
            <KV label="Sesión"    val={`${String(r.session_h).padStart(2,'0')}h`} />
          </div>

          <div className="modal-sec">Calibración</div>
          <div className="kv-grid">
            <KV label="CALR"
                val={r.calr_valido ? `${r.calr.toFixed(3)} ns` : 'no disponible'}
                cls={r.calr_valido ? 'ok' : 'muted'} />
            <KV label="CI"      val={r.ci} />
            <KV label="Tipo SW" val={r.sw_desc} />
          </div>
          {(r.esdvar_valido || r.esig_valido) && (
            <div className="kv-grid kv-grid-2" style={{ marginTop: 6 }}>
              {r.esdvar_valido && <KV label="ESDVAR [ns]" val={r.esdvar.toFixed(3)} />}
              {r.esig_valido   && <KV label="ESIG [ns]"   val={r.esig.toFixed(1)} />}
            </div>
          )}

          <div className="modal-sec">Referencia temporal</div>
          <div className="kv-grid kv-grid-2">
            <KV label="REFDELAY [s]" val={r.refdelay?.toExponential(6)} />
            <KV label="RSIG [ns]"    val={r.rsig?.toFixed(3)} />
          </div>

          {r.tmp !== null && r.tmp !== undefined && (
            <>
              <div className="modal-sec">Condiciones ambientales</div>
              <div className="kv-grid">
                <KV label="Temperatura" val={r.tmp !== null ? `${r.tmp} °C` : '—'} />
                <KV label="Humedad"     val={r.hum !== null ? `${r.hum} %`  : '—'} />
                <KV label="Presión"     val={r.pres !== null ? `${r.pres} hPa` : '—'} />
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Componente: leyenda ───────────────────────────────────────────────────────

function Leyenda() {
  const items = [
    { label: `< ${DRMS_V} ns`,           d: DRMS_V * 0.4 },
    { label: `${DRMS_V}–${DRMS_A} ns`,   d: 0.50 },
    { label: `${DRMS_A}–${DRMS_R} ns`,   d: 0.80 },
    { label: `> ${DRMS_R} ns`,           d: 1.10 },
  ];
  return (
    <div className="leyenda">
      <span>DRMS:</span>
      {items.map(it => (
        <div className="leg-item" key={it.label}>
          <div className="leg-swatch" style={{ background: drmsColor(it.d) }} />
          <span>{it.label}</span>
        </div>
      ))}

    </div>
  );
}

// ── App principal ─────────────────────────────────────────────────────────────

function App() {
  const [ficheros,   setFicheros]   = useState([]);
  const [selFile,    setSelFile]    = useState('');
  const [datos,      setDatos]      = useState(null);
  const [cargando,   setCargando]   = useState(false);
  const [modalReg,   setModalReg]   = useState(null);

  // Cargar lista de ficheros
  useEffect(() => {
    fetch('/api/ficheros')
      .then(r => r.json())
      .then(lista => {
        setFicheros(lista);
        if (lista.length > 0) setSelFile(lista[0].nombre);
      })
      .catch(() => {});
  }, []);

  // Cargar datos al cambiar fichero
  useEffect(() => {
    if (!selFile) return;
    setCargando(true);
    setDatos(null);
    fetch(`/api/dia/${encodeURIComponent(selFile)}`)
      .then(r => r.json())
      .then(d => { setDatos(d); setCargando(false); })
      .catch(() => setCargando(false));
  }, [selFile]);

  // Refresco automático cada 60 s
  useEffect(() => {
    const id = setInterval(() => {
      if (!selFile) return;
      fetch(`/api/dia/${encodeURIComponent(selFile)}`)
        .then(r => r.json())
        .then(d => setDatos(d))
        .catch(() => {});
    }, 60_000);
    return () => clearInterval(id);
  }, [selFile]);

  const s = datos?.stats;

  return (
    <div className="app">

      {/* Topbar */}
      <div className="topbar">
        <div className="brand">
          <span className="brand-title">TWSTFT</span>
          <span className="brand-sub">Real Instituto y Observatorio de la Armada</span>
        </div>
        <select className="file-select" value={selFile}
                onChange={e => setSelFile(e.target.value)}>
          {ficheros.map(f => (
            <option key={f.nombre} value={f.nombre}>
              {f.mjd ? `MJD ${f.mjd}` : f.nombre} — {f.nombre}
            </option>
          ))}
        </select>
      </div>

      {/* Stats bar */}
      <div className="statsbar">
        {s ? (
          <>
            <div className="stat">
              <span>MJD</span>
              <span className="stat-val" style={{ color: '#185FA5' }}>{s.mjd}</span>
            </div>
            <div className="stat">
              <span>Laboratorios</span>
              <span className="stat-val">{s.n_labs}</span>
            </div>
            <div className="stat">
              <span>Sesiones</span>
              <span className="stat-val">{s.n_sesiones}</span>
            </div>
            <div className="stat">
              <span>Registros</span>
              <span className="stat-val">{s.n_registros}</span>
            </div>
            <div className="stat">
              <span>DRMS medio</span>
              <span className="stat-val"
                    style={{ color: drmsColor(s.drms_medio) }}>
                {s.drms_medio.toFixed(3)} ns
              </span>
            </div>
            <div className="stat">
              <span>mín / máx</span>
              <span className="stat-val">
                <span style={{ color: drmsColor(s.drms_min) }}>{s.drms_min.toFixed(3)}</span>
                <span style={{ color: '#aaa99f' }}> / </span>
                <span style={{ color: drmsColor(s.drms_max) }}>{s.drms_max.toFixed(3)}</span>
                <span style={{ color: '#aaa99f' }}> ns</span>
              </span>
            </div>
          </>
        ) : (
          <span style={{ color: 'var(--text3)' }}>Selecciona un fichero ITU</span>
        )}
      </div>

      {/* Tabla */}
      <div className="table-wrap">
        {cargando && (
          <div className="loading">
            <div className="spinner" />
            <span>Cargando…</span>
          </div>
        )}
        {!cargando && datos && (
          <div className="table-container">
            <table>
              <thead>
                <tr>
                  <th className="th-lab">Laboratorio</th>
                  {datos.sesiones.map(sh => (
                    <th key={sh}>{String(sh).padStart(2,'0')}h</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {datos.tabla.map(fila => {
                  const esNcal = datos.sesiones.every(
                    sh => !fila.sesiones[sh] || fila.sesiones[sh].sw === 9
                  );
                  return (
                    <tr key={fila.lab}>
                      <td className="td-lab">
                        <span className="lab-name">{fila.lab}</span>
                        {esNcal && <span className="lab-ncal">sin cal.</span>}
                      </td>
                      {datos.sesiones.map(sh => (
                        <Celda
                          key={sh}
                          r={fila.sesiones[String(sh)]}
                          ncal={esNcal}
                          onClick={r => setModalReg(r)}
                        />
                      ))}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {!cargando && !datos && ficheros.length === 0 && (
          <div className="empty">
            No se encontraron ficheros ITU en el directorio configurado.
          </div>
        )}
      </div>

      {/* Leyenda */}
      <Leyenda />

      {/* Modal */}
      {modalReg && (
        <Modal r={modalReg} onClose={() => setModalReg(null)} />
      )}

    </div>
  );
}

ReactDOM.createRoot(document.getElementById('app')).render(<App />);
</script>
</body>
</html>
"""



@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_APP


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------

def main():
    global itu_dir_global

    parser = argparse.ArgumentParser(description="TWSTFT Viewer")
    parser.add_argument("--config",  default=CONFIG_DEFAULT)
    parser.add_argument("--itu-dir", default=None)
    parser.add_argument("--port",    type=int, default=8050)
    parser.add_argument("--host",    default="0.0.0.0")
    args = parser.parse_args()

    # Leer itu_dir del ini
    if args.itu_dir:
        itu_dir_global = args.itu_dir
    elif os.path.isfile(args.config):
        cfg = configparser.ConfigParser(
            interpolation=None, inline_comment_prefixes=("#",)
        )
        cfg.read(args.config, encoding="utf-8")
        try:
            base    = cfg.get("ficheros", "base").strip()
            itu_sub = cfg.get("ficheros", "itu_dir", fallback="itu").strip()
            itu_dir_global = os.path.join(base, itu_sub)
        except configparser.NoSectionError:
            pass

    # Cargar orden de labs
    orden = cargar_orden_labs(args.config)
    labs_orden_ini.extend(orden)

    print(f"Directorio ITU : {itu_dir_global}")
    print(f"Labs ordenados : {len(labs_orden_ini)}")
    print(f"Ficheros ITU   : {len(listar_ficheros())}")
    print(f"Servidor en    : http://{args.host}:{args.port}")

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
