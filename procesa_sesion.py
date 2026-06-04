#!/usr/bin/env python3
"""
procesa_sesion.py
=================
Procesa una sesión TWSTFT de 2 horas (hora par + hora impar) a partir
del fichero raw generado por lee_satres.py y produce el fichero de datos
en formato ITU (Anexo 2, Recomendación UIT-R TF.1153-4).

Estructura de una sesión:
  - Hora par   (HH:00 - HH:59): laboratorios oficiales  (minutos  0-59)
  - Hora impar (HH+1:00-HH+1:59): calibraciones/pruebas (minutos 60-119)

Cada slot dura 3 minutos:
  - Minuto 0 del slot: enganche  → datos descartados
  - Minutos 1 y 2:    medidas válidas (máx. 120 muestras)

Criterio de validez de slot:
  - Solo se usan líneas en modo long (campo formato == 'L')
  - n < 50 muestras antes del ajuste → descartar slot
  - Ajuste cuadrático + filtro iterativo 5-sigma
  - n < 50 muestras tras el filtrado  → descartar slot
  - Punto de evaluación del polinomio: t = 59.5 s desde inicio de medidas

Implementación:
  - Las 2 horas de la sesión se leen en un único DataFrame (pandas)
  - Cada slot se obtiene filtrando ese DataFrame por minuto_sesion y formato
  - El ajuste cuadrático y el sigma-clipping operan sobre arrays numpy

Nomenclatura del fichero ITU de salida:
  TW<LAB><MM>.<MMM>  (últimas 5 cifras del MJD, p.ej. TWROA61.140)

Uso:
  python3 procesa_sesion.py --hora HHMM [--fecha YYYYMMDD]
                            [--config RUTA_INI] [--debug]

  --hora    Hora par de inicio de la sesión (p.ej. 0000, 0200)
  --fecha   Fecha YYYYMMDD (defecto: ayer)
  --config  Ruta a twstft.ini (defecto: mismo dir que el script)
  --debug   Log detallado en consola

Ejemplo cron (02:00 → procesa sesión 00:00-01:59):
  0 2,4,6,8,10,12,14,16,18,20,22,0 * * * \\
      python3 procesa_sesion.py --hora $(date -d '2 hours ago' +%%H%%M)
"""

import os
import sys
import re
import argparse
import logging
import logging.handlers
import configparser
from datetime import date, datetime, timedelta
from typing import Optional
from collections import defaultdict

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Versión
# ---------------------------------------------------------------------------
__version__ = "1.3"

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
CONFIG_FILENAME  = "twstft.ini"
BASEDIR_DEFAULT  = "/var/log/satres"
MIN_MUESTRAS     = 50
NTL_NOMINAL      = 119
T_EVAL           = 59.5       # punto de evaluación del ajuste [s]
SIGMA_FILTRO     = 5.0
RECEPTOR_DEFAULT = "Rx1"

# Columnas del DataFrame raw
COL_HORA     = "hora"
COL_MINUTO   = "minuto"
COL_SEGUNDO  = "segundo"
COL_MIN_SES  = "min_sesion"   # minuto dentro de la sesión (0-119)
COL_SLOT     = "slot_start"   # minuto de inicio del slot (0,3,6,...,117)
COL_FORMATO  = "formato"      # 'L' o 'S'
COL_PRN      = "prn"
COL_RTT      = "rtt_ns"
COL_T        = "t_seg"        # segundos desde inicio de medidas del slot

# Valores ITU para datos no disponibles
TMP_ND  = "+99"
HUM_ND  = "999"
PRES_ND = "9999"

# Meses en español para el nombre del fichero .ema
MESES_ES = {
    1: "ene", 2: "feb", 3: "mar", 4: "abr",
    5: "may", 6: "jun", 7: "jul", 8: "ago",
    9: "sep", 10: "oct", 11: "nov", 12: "dic",
}

# MJD
_MJD_EPOCH = date(1858, 11, 17)

def fecha_a_mjd(d: date) -> int:
    return (d - _MJD_EPOCH).days

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

def cargar_config(ruta: str) -> configparser.ConfigParser:
    if not os.path.isfile(ruta):
        raise FileNotFoundError(f"Fichero de configuración no encontrado: {ruta}")
    cfg = configparser.ConfigParser(
        interpolation=None,
        inline_comment_prefixes=("#",),
    )
    cfg.read(ruta, encoding="utf-8")
    return cfg


def get_str(cfg: configparser.ConfigParser, sec: str,
            key: str, fallback: str = "") -> str:
    try:    return cfg.get(sec, key).strip()
    except: return fallback

def get_float(cfg: configparser.ConfigParser, sec: str,
              key: str, fallback: float = 0.0) -> float:
    try:    return cfg.getfloat(sec, key)
    except: return fallback

def get_int(cfg: configparser.ConfigParser, sec: str,
            key: str, fallback: int = 0) -> int:
    try:    return cfg.getint(sec, key)
    except: return fallback


def cargar_labs(cfg: configparser.ConfigParser) -> dict:
    """Carga todos los laboratorios de secciones [lab XXXX]."""
    labs = {}
    for sec in cfg.sections():
        if not sec.lower().startswith("lab "):
            continue
        nombre   = get_str(cfg, sec, "nombre",   sec[4:].strip())
        minuto   = get_int(cfg, sec, "minuto",   -1)
        prn      = get_int(cfg, sec, "prn",      -1)
        link     = get_int(cfg, sec, "link",      0)
        sw       = get_int(cfg, sec, "sw",        9)
        estacion = get_str(cfg, sec, "estacion", "")

        def safe_float(raw, default):
            try:    return float(raw)
            except: return default

        calr    = safe_float(get_str(cfg, sec, "calr",     "999999999"), 999999999.0)
        ci_raw  = get_str(cfg, sec, "ci", "999")
        ci      = int(ci_raw) if ci_raw.lstrip('-').isdigit() else 999
        esdvar  = safe_float(get_str(cfg, sec, "esdvar",   "999999999"), 999999999.0)
        esig    = safe_float(get_str(cfg, sec, "esig",     "99999"),     99999.0)
        rsig    = safe_float(get_str(cfg, sec, "rsig",     "99999"),     99999.0)
        refdelay= safe_float(get_str(cfg, sec, "refdelay", "0"),         0.0)

        labs[nombre] = dict(
            nombre=nombre, minuto=minuto, prn=prn, link=link,
            sw=sw, calr=calr, ci=ci, esdvar=esdvar,
            esig=esig, rsig=rsig, refdelay=refdelay,
            estacion=estacion,
        )
    return labs


def construir_indice_slots(labs: dict) -> dict:
    """Construye índice (minuto_slot, prn) → lab."""
    return {(lab["minuto"], lab["prn"]): lab for lab in labs.values()}


# ---------------------------------------------------------------------------
# Lectura del fichero raw → DataFrame
# ---------------------------------------------------------------------------

def leer_raw_a_dataframe(ruta_raw: str, fecha_str: str,
                         hora_par: int, receptor: str) -> pd.DataFrame:
    """
    Lee únicamente las 2 horas de la sesión del fichero raw y construye
    un DataFrame con las columnas necesarias para el procesado.

    Solo se incluyen líneas del receptor configurado, en modo long ('L'),
    con RTT != 0, y que no pertenezcan al minuto de enganche del slot.

    Columnas del DataFrame resultante:
      hora, minuto, segundo, min_sesion, slot_start,
      formato, prn, rtt_ns, t_seg
    """
    prefijo    = f"%{receptor}>{fecha_str};"
    hora_impar = hora_par + 1
    horas_sesion = {hora_par, hora_impar}

    registros = []

    with open(ruta_raw, "r", errors="replace") as f:
        for linea in f:
            linea = linea.strip().replace("\r", "")

            if not linea.startswith(prefijo):
                continue

            partes = linea.split(";")
            if len(partes) < 13:
                continue

            # --- Hora ---
            hms = partes[1]
            try:
                h = int(hms[:2])
                m = int(hms[3:5])
                s = int(hms[6:8])
            except (ValueError, IndexError):
                continue

            if h not in horas_sesion:
                continue

            # --- Formato: solo long ---
            fmt = partes[6].strip().upper()
            if fmt != "L":
                continue

            # --- Minuto de sesión y slot ---
            min_ses    = (h - hora_par) * 60 + m
            slot_start = (min_ses // 3) * 3

            # Minuto de enganche → descartar
            if min_ses == slot_start:
                continue

            # --- PRN ---
            try:
                prn = int(partes[10].strip())
            except (ValueError, IndexError):
                continue

            # --- RTT raw ---
            try:
                rtt = float(partes[12].strip())
            except (ValueError, IndexError):
                continue

            if rtt == 0.0:
                continue

            # --- t_seg: segundos desde inicio de medidas del slot ---
            t_abs = h * 3600 + m * 60 + s
            t_ref = hora_par * 3600 + slot_start * 60 + 60
            t_seg = t_abs - t_ref

            registros.append({
                COL_HORA:    h,
                COL_MINUTO:  m,
                COL_SEGUNDO: s,
                COL_MIN_SES: min_ses,
                COL_SLOT:    slot_start,
                COL_FORMATO: fmt,
                COL_PRN:     prn,
                COL_RTT:     rtt,
                COL_T:       t_seg,
            })

    if not registros:
        return pd.DataFrame(columns=[
            COL_HORA, COL_MINUTO, COL_SEGUNDO, COL_MIN_SES,
            COL_SLOT, COL_FORMATO, COL_PRN, COL_RTT, COL_T,
        ])

    df = pd.DataFrame(registros)

    # Tipos explícitos
    df[COL_HORA]    = df[COL_HORA].astype(np.int8)
    df[COL_MINUTO]  = df[COL_MINUTO].astype(np.int8)
    df[COL_SEGUNDO] = df[COL_SEGUNDO].astype(np.int8)
    df[COL_MIN_SES] = df[COL_MIN_SES].astype(np.int8)
    df[COL_SLOT]    = df[COL_SLOT].astype(np.int8)
    df[COL_PRN]     = df[COL_PRN].astype(np.int16)
    df[COL_RTT]     = df[COL_RTT].astype(np.float64)
    df[COL_T]       = df[COL_T].astype(np.int16)

    logging.debug("DataFrame raw: %d filas, %d slots únicos",
                  len(df), df[COL_SLOT].nunique())
    return df


# ---------------------------------------------------------------------------
# Ajuste cuadrático con filtro iterativo 5-sigma
# ---------------------------------------------------------------------------

def ajuste_cuadratico_5sigma(df_slot: pd.DataFrame,
                              min_muestras: int = MIN_MUESTRAS,
                              sigma: float = SIGMA_FILTRO,
                              t_eval: float = T_EVAL):
    """
    Ajuste cuadrático con filtro iterativo 5-sigma sobre un DataFrame de slot.

    Entrada:
      df_slot   : DataFrame con columnas t_seg y rtt_ns (solo medidas válidas)
      min_muestras, sigma, t_eval : parámetros del algoritmo

    Algoritmo:
      0. Si n < min_muestras → None
      1. Ajuste cuadrático (numpy.polyfit grado 2)
      2. Residuos y σ
      3. Descartar |residuo| > sigma·σ
      4. Si quedan < min_muestras → None
      5. Si se descartó algo → repetir desde 1
      6. Convergencia → evaluar en t_eval

    Devuelve:
      (tw_ns, drms_ns, smp, atl_s, df_usado, n_iter) o None si descartado.
    """
    # Paso 0
    if len(df_slot) < min_muestras:
        return None

    df_activo = df_slot.copy()
    n_iter = 0

    while True:
        n_iter += 1
        t_arr = df_activo[COL_T].to_numpy(dtype=np.float64)
        y_arr = df_activo[COL_RTT].to_numpy(dtype=np.float64)

        # Ajuste cuadrático
        coeffs   = np.polyfit(t_arr, y_arr, 2)
        p        = np.poly1d(coeffs)
        residuos = y_arr - p(t_arr)
        std      = np.std(residuos, ddof=1)  # desviación estándar muestral (N-1)

        # Máscara de outliers
        mascara_ok = np.abs(residuos) <= sigma * std

        if mascara_ok.all():
            # Convergencia
            tw_ns   = float(p(t_eval))
            drms_ns = float(np.sqrt(np.mean(residuos**2)))
            atl_s   = int(t_arr[-1] - t_arr[0])
            smp     = len(df_activo)
            return tw_ns, drms_ns, smp, atl_s, df_activo, n_iter

        # Filtrar outliers del DataFrame activo
        df_activo = df_activo[mascara_ok].reset_index(drop=True)

        # Validación tras filtrado
        if len(df_activo) < min_muestras:
            return None


# ---------------------------------------------------------------------------
# Generación del encabezamiento ITU (Anexo 2)
# ---------------------------------------------------------------------------

def generar_encabezamiento(cfg: configparser.ConfigParser,
                           nombre_fichero: str) -> list:
    """
    Genera las líneas de encabezamiento del fichero ITU replicando
    exactamente el formato del fichero oficial (espaciado fijo).
    """
    lab       = get_str(cfg, "itu", "lab",       "???")
    fmt       = get_str(cfg, "itu", "format",    "01")
    rev_date  = get_str(cfg, "itu", "rev_date",  "")
    ref_frame = get_str(cfg, "itu", "ref_frame", "WGS-84")
    loc_mon   = get_str(cfg, "itu", "loc_mon",   "NO")
    modem     = get_str(cfg, "itu", "modem",     "")
    comments  = get_str(cfg, "itu", "comments",  "")

    lineas = []
    # Formato oficial: keyword alineada a columna 12
    lineas.append(f"* {nombre_fichero}")
    lineas.append(f"* FORMAT    {fmt}")
    lineas.append(f"* LAB       {lab}")
    if rev_date:
        lineas.append(f"* REV DATE  {rev_date}")

    # Estaciones terrenas — columnas fijas: LA: en col 12, LO: en col 37, HT: en col 59
    for sec in cfg.sections():
        if not sec.lower().startswith("estacion "):
            continue
        nombre   = get_str(cfg, sec, "nombre",   sec[9:].strip())
        latitud  = get_str(cfg, sec, "latitud",  "")
        longitud = get_str(cfg, sec, "longitud", "")
        altura   = get_str(cfg, sec, "altura",   "0")
        s = f"* ES {nombre:<6} LA: {latitud}"
        s = s.ljust(37) + f"LO: {longitud}"
        s = s.ljust(59) + f"HT: +{float(altura):07.2f} m"
        lineas.append(s)

    lineas.append(f"* REF-FRAME {ref_frame}")

    # Links satelitales — columnas fijas: SAT: en col 12, NLO: en col 37, XPNDR: en col 59
    for sec in cfg.sections():
        if not sec.lower().startswith("link "):
            continue
        link_id  = sec[5:].strip()
        satelite = get_str(cfg, sec, "satelite", "")
        nlo_dir  = get_str(cfg, sec, "nlo_dir",  "E")
        nlo_grad = get_str(cfg, sec, "nlo_grad", "0")
        nlo_min  = get_str(cfg, sec, "nlo_min",  "00")
        nlo_seg  = get_str(cfg, sec, "nlo_seg",  "00.000")
        xpndr    = get_str(cfg, sec, "xpndr",    "999999999")
        sat_ntx  = get_float(cfg, sec, "sat_ntx", 0.0)
        sat_nrx  = get_float(cfg, sec, "sat_nrx", 0.0)
        bw       = get_float(cfg, sec, "bw",      0.0)
        nlo_str  = f"{nlo_dir} {int(nlo_grad):3d} {int(nlo_min):2d} {nlo_seg}"
        s = f"* LINK {link_id:>4} SAT: {satelite}"
        s = s.ljust(37) + f"NLO: {nlo_str}"
        s = s.ljust(59) + f"XPNDR: {xpndr} ns"
        lineas.append(s)
        lineas.append(
            f"*           SAT-NTX: {sat_ntx:10.4f} MHz  "
            f"SAT-NRX: {sat_nrx:10.4f} MHz  BW: {bw:5.1f} MHz"
        )

    # Calibraciones — columnas fijas: TYPE: en col 12, MJD: en col 37, EST. en col 49
    for sec in cfg.sections():
        if not sec.lower().startswith("cal "):
            continue
        cal_id = sec[4:].strip()
        tipo   = get_str(cfg,  sec, "tipo",   "")
        mjd_c  = get_int(cfg,  sec, "mjd",    0)
        incert = get_float(cfg, sec, "incert", 0.0)
        s = f"* CAL {cal_id:>5} TYPE: {tipo}"
        s = s.ljust(37) + f"MJD: {mjd_c:5d}"
        s = s.ljust(49) + f"EST. UNCERT.: {incert:8.3f} ns"
        lineas.append(s)

    lineas.append(f"* LOC-MON   {loc_mon}")
    lineas.append(f"* MODEM     {modem}")
    if comments:
        lineas.append(f"* COMMENTS  {comments}")
    lineas.append("*")

    # Cabecera de columnas — replicar exactamente el formato oficial
    lineas.append(
        "* EARTH-STAT  LI  MJD  STTIME NTL        TW        DRMS SMP ATL"
        "     REFDELAY     RSIG  CI S    CALR     ESDVAR   ESIG TMP HUM PRES"
    )
    lineas.append(
        "* LOC    REM           hhmmss  s         s          ns       s"
        "         s          ns            ns        ns      ns degC  %  mbar"
    )
    return lineas


# ---------------------------------------------------------------------------
# Formateo de una línea de datos ITU
# ---------------------------------------------------------------------------

def formatear_linea_itu(loc: str, rem: str, li: str, mjd: int,
                         sttime: str, ntl: int,
                         tw_s: float, drms_ns: float, smp: int, atl: int,
                         refdelay_s: float, rsig: float, ci: int, sw: int,
                         calr: float, esdvar: float, esig: float,
                         tmp: str = TMP_ND, hum: str = HUM_ND,
                         pres: str = PRES_ND) -> str:
    """
    Formatea una línea de datos según el Anexo 2 de UIT-R TF.1153-4.
    Espaciados verificados contra fichero oficial:
      - 1 espacio entre la mayoría de campos
      - CALR y ESDVAR justificados a derecha en 9 chars
      - 2 espacios antes de TMP y antes de HUM
    """
    tw_str     = f"{tw_s:+.12f}"       # +0.262383672844 = 15 chars
    ref_str    = f"{refdelay_s:+.12f}" # +0.000000964858 = 15 chars
    rsig_str   = f"{rsig:.3f}"   if rsig  < 99998 else "99999"
    esig_str   = f"{esig:.3f}"   if esig  < 99998 else "99999"
    calr_str   = f"{calr:.3f}"   if abs(calr)   < 999999998 else "999999999"
    esdvar_str = f"{esdvar:.3f}" if abs(esdvar) < 999999998 else "999999999"

    return (
        f"{loc:>6} {rem:>6} {li:>2} {mjd:5d} {sttime:6s} {ntl:3d} "
        f"{tw_str} {drms_ns:.3f} {smp:03d} {atl:03d} "
        f"{ref_str} {rsig_str} {ci:3d} {sw:1d} "
        f"{calr_str:>9} {esdvar_str:>9} {esig_str:>5}  {tmp:>2}  {hum:>2} {pres}"
    )


# ---------------------------------------------------------------------------
# Nombre del fichero ITU
# ---------------------------------------------------------------------------

def nombre_fichero_itu(lab: str, mjd: int) -> str:
    """TW<LAB><MM>.<MMM> donde MM.MMM son las últimas 5 cifras del MJD."""
    mjd_str = f"{mjd:05d}"
    return f"tw{lab.lower()}{mjd_str[-5:-3]}.{mjd_str[-3:]}"


# ---------------------------------------------------------------------------
# Datos ambientales (.ema)
# ---------------------------------------------------------------------------

def nombre_fichero_ema(fecha: date) -> str:
    """
    Genera el nombre del fichero .ema para la fecha indicada.
    Formato: DDmmmAA.ema  (p.ej. 10abr26.ema)
    """
    dia = f"{fecha.day:02d}"
    mes = MESES_ES[fecha.month]
    ano = fecha.strftime("%y")
    return f"{dia}{mes}{ano}.ema"


def leer_ema(ruta_ema: str) -> pd.DataFrame:
    """
    Lee el fichero .ema y devuelve un DataFrame con columnas:
      minuto, temperatura, humedad, presion

    El fichero tiene 9 columnas separadas por espacios/tabuladores.
    Usamos: col[0]=minuto, col[1]=temperatura, col[2]=humedad, col[7]=presion.
    Las medidas están cada 10 minutos.
    Devuelve DataFrame vacío si el fichero no existe o hay error.
    """
    if not os.path.isfile(ruta_ema):
        logging.warning("Fichero .ema no encontrado: %s", ruta_ema)
        return pd.DataFrame(columns=["minuto", "temperatura", "humedad", "presion"])

    registros = []
    try:
        with open(ruta_ema, "r", errors="replace") as f:
            for linea in f:
                linea = linea.strip()
                if not linea or linea.startswith("#"):
                    continue
                cols = linea.split()
                if len(cols) < 8:
                    continue
                try:
                    registros.append({
                        "minuto":      float(cols[0]),
                        "temperatura": float(cols[1]),
                        "humedad":     float(cols[2]),
                        "presion":     float(cols[7]),
                    })
                except (ValueError, IndexError):
                    continue
    except Exception as exc:
        logging.error("Error leyendo fichero .ema %s: %s", ruta_ema, exc)
        return pd.DataFrame(columns=["minuto", "temperatura", "humedad", "presion"])

    if not registros:
        logging.warning("Fichero .ema vacío o sin datos válidos: %s", ruta_ema)
        return pd.DataFrame(columns=["minuto", "temperatura", "humedad", "presion"])

    df = pd.DataFrame(registros)
    df = df.sort_values("minuto").reset_index(drop=True)
    logging.debug("EMA: %d medidas leídas de %s", len(df), ruta_ema)
    return df


def interpolar_ema(df_ema: pd.DataFrame, minuto_fit: float):
    """
    Interpola los valores ambientales para el instante minuto_fit
    (minuto del día, puede tener decimales).

    Reglas:
      - Si df_ema está vacío → devuelve (TMP_ND, HUM_ND, PRES_ND)
      - Si minuto_fit <= primera medida → usa la primera medida
      - Si minuto_fit >= última medida  → usa la última medida (sin extrapolar)
      - En otro caso → interpolación lineal entre las dos medidas adyacentes

    Devuelve (tmp_str, hum_str, pres_str) en formato ITU.
    """
    if df_ema.empty:
        return TMP_ND, HUM_ND, PRES_ND

    minutos = df_ema["minuto"].to_numpy()
    temps   = df_ema["temperatura"].to_numpy()
    humeds  = df_ema["humedad"].to_numpy()
    presis  = df_ema["presion"].to_numpy()

    # Antes o en el primer punto
    if minuto_fit <= minutos[0]:
        t, h, p = temps[0], humeds[0], presis[0]

    # Más allá del último punto → usar última medida
    elif minuto_fit >= minutos[-1]:
        t, h, p = temps[-1], humeds[-1], presis[-1]

    # Interpolación lineal
    else:
        # Índice del primer punto mayor que minuto_fit
        idx = int(np.searchsorted(minutos, minuto_fit, side="right"))
        idx = min(idx, len(minutos) - 1)
        m0, m1 = minutos[idx-1], minutos[idx]
        frac = (minuto_fit - m0) / (m1 - m0)
        t = temps[idx-1]   + frac * (temps[idx]   - temps[idx-1])
        h = humeds[idx-1]  + frac * (humeds[idx]  - humeds[idx-1])
        p = presis[idx-1]  + frac * (presis[idx]  - presis[idx-1])

    # Formatear según formato ITU
    # TMP:  nn    (°C, entero, sin signo aunque sea negativo salvo <0)
    # HUM:  nnn   (%, entero sin decimales)
    # PRES: nnnn  (hPa, entero)
    tmp_str  = f"{int(round(t))}"
    hum_str  = f"{int(round(h))}"
    pres_str = f"{int(round(p))}"

    return tmp_str, hum_str, pres_str


# ---------------------------------------------------------------------------
# Proceso principal de una sesión
# ---------------------------------------------------------------------------

def procesar_sesion(cfg: configparser.ConfigParser,
                    fecha: date,
                    hora_par: int,
                    debug: bool,
                    ruta_raw_override: Optional[str] = None,
                    dia_completo: bool = False) -> None:
    """
    Procesa la sesión de 2 horas que comienza en hora_par del día fecha.

    ruta_raw_override: si se especifica, usa ese fichero raw directamente
                       en lugar de buscar en <basedir>/<unit_id>/raw.YYYYMMDD
    dia_completo     : si True y es la primera sesión (hora_par==0), borra el
                       fichero ITU existente antes de empezar para regenerarlo
                       desde cero
    """
    basedir     = get_str(cfg, "ficheros", "base",      BASEDIR_DEFAULT)
    raw_dir     = get_str(cfg, "ficheros", "raw_dir",   "raw")
    ema_dir     = get_str(cfg, "ficheros", "ema_dir",   "ema")
    itu_dir     = get_str(cfg, "ficheros", "itu_dir",   "itu")
    lab         = get_str(cfg, "itu",      "lab",       "???")
    ntl         = get_int(cfg, "itu",      "ntl",       NTL_NOMINAL)
    receptor    = RECEPTOR_DEFAULT
    local_par   = get_str(cfg, "local",    "par",       "")
    local_impar = get_str(cfg, "local",    "impar",     "")

    # Unit ID desde [itu] modem
    modem_str = get_str(cfg, "itu", "modem", "")
    m = re.search(r'(\d+)\s*$', modem_str)
    unit_id = m.group(1).zfill(3) if m else "000"

    fecha_str_raw  = fecha.strftime("%Y/%m/%d")
    fecha_str_file = fecha.strftime("%Y%m%d")

    # Resolver ruta del fichero raw
    if ruta_raw_override:
        ruta_raw = ruta_raw_override
        # Si se pasa un raw explícito intentamos inferir la fecha de su nombre
        # (raw.YYYYMMDD) para no depender de --fecha en ese caso
        basename = os.path.basename(ruta_raw)
        m_fecha = re.search(r'(\d{8})$', basename)
        if m_fecha:
            try:
                fecha_raw = datetime.strptime(m_fecha.group(1), "%Y%m%d").date()
                fecha_str_raw  = fecha_raw.strftime("%Y/%m/%d")
                fecha_str_file = fecha_raw.strftime("%Y%m%d")
                fecha = fecha_raw
            except ValueError:
                pass  # usar la fecha proporcionada por --fecha
    else:
        ruta_raw = os.path.join(basedir, unit_id, raw_dir, f"raw.{fecha_str_file}")

    if not os.path.isfile(ruta_raw):
        logging.error("Fichero raw no encontrado: %s", ruta_raw)
        sys.exit(1)

    logging.info("Sesión %s %02d:00-%02d:59 | raw: %s",
                 fecha_str_file, hora_par, hora_par + 1, ruta_raw)

    # --- Cargar datos ambientales (.ema) ---
    df_ema = pd.DataFrame(columns=["minuto", "temperatura", "humedad", "presion"])
    nombre_ema = nombre_fichero_ema(fecha)
    ruta_ema   = os.path.join(basedir, unit_id, ema_dir, nombre_ema)
    df_ema     = leer_ema(ruta_ema)
    if not df_ema.empty:
        logging.info("EMA: %s (%d medidas)", ruta_ema, len(df_ema))
    else:
        logging.warning("EMA no disponible, se usarán valores por defecto")

    # --- Cargar labs e índice ---
    labs      = cargar_labs(cfg)
    idx_slots = construir_indice_slots(labs)

    # --- Parámetros del laboratorio local (refdelay y rsig son del local) ---
    lab_local_par   = labs.get(local_par,   {})
    lab_local_impar = labs.get(local_impar, {})

    # --- Leer raw → DataFrame (solo las 2 horas de la sesión) ---
    df_raw = leer_raw_a_dataframe(ruta_raw, fecha_str_raw, hora_par, receptor)

    if df_raw.empty:
        logging.warning("No se encontraron datos para esta sesión.")
        return

    logging.info("DataFrame: %d muestras válidas, %d slots",
                 len(df_raw), df_raw[COL_SLOT].nunique())

    mjd = fecha_a_mjd(fecha)

    # --- Procesar cada slot ---
    lineas_datos = []

    for slot_start_raw, df_grupo in df_raw.groupby(COL_SLOT):
        slot_start: int = int(slot_start_raw)  # type: ignore[arg-type]

        # Cada grupo puede tener varios PRN si el enganche fue tardío;
        # tomamos el PRN mayoritario dentro del grupo
        prn = int(df_grupo[COL_PRN].mode().iloc[0])

        lab_info = idx_slots.get((slot_start, prn))
        if lab_info is None:
            logging.warning("Slot min=%3d PRN=%2d: lab no encontrado en config",
                            slot_start, prn)
            continue

        nombre_lab = lab_info["nombre"]

        # DataFrame del slot (ya filtrado: solo long, no enganche, rtt!=0)
        df_slot = df_grupo[df_grupo[COL_PRN] == prn].reset_index(drop=True)

        # Ajuste cuadrático con filtro 5-sigma
        resultado = ajuste_cuadratico_5sigma(df_slot, MIN_MUESTRAS,
                                              SIGMA_FILTRO, T_EVAL)
        if resultado is None:
            n = len(df_slot)
            razon = "< 50 inicial" if n < MIN_MUESTRAS else "< 50 tras 5σ"
            logging.info("  SKIP  min=%3d %-8s: descartado (%s, n=%d)",
                         slot_start, nombre_lab, razon, n)
            continue

        tw_ns, drms_ns, smp, atl, df_usado, n_iter = resultado
        descartadas = len(df_slot) - smp
        tw_s = tw_ns * 1e-9

        logging.info("  OK    min=%3d %-8s: TW=%+.6f ns  DRMS=%.4f  "
                     "SMP=%d  ATL=%d  iter=%d  desc=%d",
                     slot_start, nombre_lab, tw_ns, drms_ns,
                     smp, atl, n_iter, descartadas)

        # STTIME: HH:MM:SS inicio de medidas
        t_med = hora_par * 3600 + slot_start * 60 + 60
        sttime = f"{t_med//3600:02d}{(t_med%3600)//60:02d}{t_med%60:02d}"
        loc = local_par if slot_start < 60 else local_impar

        # Minuto del día correspondiente al punto de fit (t_eval=59.5 s
        # desde el inicio de medidas del slot)
        t_fit_abs  = t_med + T_EVAL           # segundos desde medianoche
        minuto_fit = t_fit_abs / 60.0         # minuto del día (con decimales)

        # Interpolar datos ambientales para el instante del fit
        tmp_str, hum_str, pres_str = interpolar_ema(df_ema, minuto_fit)

        # refdelay y rsig son siempre del laboratorio local
        lab_local = lab_local_par if slot_start < 60 else lab_local_impar
        refdelay_local = lab_local.get("refdelay", 0.0)
        rsig_local     = lab_local.get("rsig",     99999.0)

        lineas_datos.append(formatear_linea_itu(
            loc=loc,
            rem=nombre_lab,
            li=f"{lab_info['link']:02d}",
            mjd=mjd,
            sttime=sttime,
            ntl=ntl,
            tw_s=tw_s,
            drms_ns=drms_ns,
            smp=smp,
            atl=atl,
            refdelay_s=refdelay_local,
            rsig=rsig_local,
            ci=lab_info["ci"],
            sw=lab_info["sw"],
            calr=lab_info["calr"],
            esdvar=lab_info["esdvar"],
            esig=lab_info["esig"],
            tmp=tmp_str,
            hum=hum_str,
            pres=pres_str,
        ))

    if not lineas_datos:
        logging.warning("No se generaron líneas de datos para esta sesión.")
        return

    # --- Escribir fichero ITU ---
    nombre_itu = nombre_fichero_itu(lab, mjd)
    dir_salida = os.path.join(basedir, unit_id, itu_dir)
    os.makedirs(dir_salida, exist_ok=True)
    ruta_itu = os.path.join(dir_salida, nombre_itu)

    # Si se está procesando el día completo y es la primera sesión (hora_par==0),
    # borrar el fichero ITU existente para regenerarlo desde cero sin datos viejos
    if dia_completo and hora_par == 0 and os.path.isfile(ruta_itu):
        os.remove(ruta_itu)
        logging.info("Fichero ITU anterior eliminado: %s", ruta_itu)

    # Si el fichero no existe aún → escribir encabezamiento + cabecera de columnas
    # Si ya existe (sesiones anteriores del mismo día) → append solo datos
    fichero_nuevo = not os.path.isfile(ruta_itu)

    with open(ruta_itu, "a", encoding="utf-8") as f:
        if fichero_nuevo:
            encabezamiento = generar_encabezamiento(cfg, nombre_itu)
            for linea in encabezamiento:
                f.write(linea + "\n")

        for linea in lineas_datos:
            f.write(linea + "\n")

    logging.info("Fichero ITU %s: %s (%d líneas de datos)",
                 "creado" if fichero_nuevo else "actualizado",
                 ruta_itu, len(lineas_datos))


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def configurar_logging(basedir: str, debug: bool) -> None:
    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logger = logging.getLogger()
    logger.handlers.clear()

    if debug:
        logger.setLevel(logging.DEBUG)
        h = logging.StreamHandler(sys.stdout)
    else:
        logger.setLevel(logging.INFO)
        log_dir = os.path.join(basedir, "log")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "procesa_sesion.log")
        h = logging.handlers.TimedRotatingFileHandler(
            filename=log_path,
            when="midnight", interval=1, backupCount=30,
            encoding="utf-8", utc=True,
        )
        h.suffix = "%Y%m%d"

    h.setFormatter(fmt)
    logger.addHandler(h)


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------

def main() -> None:
    script_dir     = os.path.dirname(os.path.abspath(__file__))
    config_default = os.path.join(script_dir, CONFIG_FILENAME)

    parser = argparse.ArgumentParser(
        description="procesa_sesion.py — Genera fichero ITU a partir del raw SATRE"
    )
    parser.add_argument(
        "--hora", type=str, default=None,
        help="Hora par de inicio de la sesión en formato HHMM (p.ej. 0000, 0200). "
             "Si se omite, se procesan todas las sesiones del día (00:00-22:00)"
    )
    parser.add_argument(
        "--fecha", type=str, default=None,
        help="Fecha YYYYMMDD (defecto: ayer)"
    )
    parser.add_argument(
        "--config", type=str, default=config_default,
        help=f"Ruta al fichero de configuración (defecto: {config_default})"
    )
    parser.add_argument(
        "--raw", type=str, default=None,
        help="Ruta explícita al fichero raw a procesar. Si se omite, se busca "
             "en <basedir>/<unit_id>/raw.YYYYMMDD según --fecha"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Log detallado en consola"
    )
    parser.add_argument(
        "--version", action="version",
        version=f"procesa_sesion.py {__version__}",
    )
    args = parser.parse_args()

    # Parsear hora(s) a procesar
    if args.hora is not None:
        hora_str = args.hora.strip().zfill(4)
        try:
            hora_par = int(hora_str[:2])
            if hora_par % 2 != 0:
                raise ValueError("La hora debe ser par")
        except ValueError as e:
            print(f"Error en --hora: {e}", file=sys.stderr)
            sys.exit(1)
        horas_a_procesar = [hora_par]
    else:
        # Sin --hora → procesar todas las sesiones del día (00, 02, ..., 22)
        horas_a_procesar = list(range(0, 24, 2))

    # Parsear fecha
    if args.fecha:
        try:
            fecha = datetime.strptime(args.fecha, "%Y%m%d").date()
        except ValueError:
            print("Error en --fecha: formato esperado YYYYMMDD", file=sys.stderr)
            sys.exit(1)
    else:
        fecha = date.today() - timedelta(days=1)

    # --raw sin --hora → procesar todas las sesiones del día sobre ese fichero
    if args.raw and args.hora is None:
        horas_a_procesar = list(range(0, 24, 2))

    # Cargar config (logging provisional hasta tener basedir)
    logging.basicConfig(level=logging.DEBUG,
                        format="%(asctime)s %(levelname)-8s %(message)s",
                        stream=sys.stdout)
    try:
        cfg = cargar_config(args.config)
    except FileNotFoundError as e:
        logging.error("%s", e)
        sys.exit(1)

    basedir = get_str(cfg, "ficheros", "base", BASEDIR_DEFAULT)
    configurar_logging(basedir, args.debug)

    logging.info("=" * 60)
    logging.info("procesa_sesion.py v%s", __version__)
    logging.info("Config  : %s", args.config)
    logging.info("Fecha   : %s", fecha.strftime("%Y-%m-%d"))
    if len(horas_a_procesar) == 1:
        logging.info("Hora par: %02d:00", horas_a_procesar[0])
    else:
        logging.info("Horas   : todas las sesiones del día (00-22)")
    if args.raw:
        logging.info("Raw     : %s (explícito)", args.raw)
    logging.info("=" * 60)

    for hora_par in horas_a_procesar:
        procesar_sesion(cfg, fecha, hora_par, args.debug,
                        ruta_raw_override=args.raw,
                        dia_completo=(len(horas_a_procesar) > 1))


if __name__ == "__main__":
    main()
    