#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
calcula_diff.py
===============
Calcula las diferencias de tiempo UTC(ROA) - UTC(lab_remoto) entre el
laboratorio local ROA y uno o varios laboratorios remotos, a partir de
sus ficheros ITU (Anexo 2, Recomendación UIT-R TF.1153-4).

El programa:
  1. A partir del fichero ITU de ROA y una ventana de días, deduce
     automáticamente los ficheros de los días anteriores.
  2. Para cada laboratorio remoto busca sus ficheros en el directorio
     especificado (insensible a mayúsculas/minúsculas).
  3. Calcula las diferencias y genera ficheros .dat por par y día.
  4. Genera una gráfica única con todos los laboratorios:
       - HTML interactivo (plotly)
       - PNG para informes (matplotlib)

Ecuación (UIT-R TF.1153-4, sección 8.2):

  S=1 (calibrado):
    UTC(1)-UTC(2) = +0.5·[TW(1)+ESDVAR(1)] + REFDELAY(1)
                    -0.5·[TW(2)+ESDVAR(2)] - REFDELAY(2)
                    +0.5·[CALR(1,2) - CALR(2,1)]

  S=9 (no calibrado):
    UTC(1)-UTC(2)+K = +0.5·[TW(1)+ESDVAR(1)] + REFDELAY(1)
                      -0.5·[TW(2)+ESDVAR(2)] - REFDELAY(2)

  S_A != S_B → inconsistencia: slot descartado con aviso.

Uso:
  python3 calcula_diff.py --roa FICHERO_ITU_ROA
                          --remoto LAB:DIRECTORIO [--remoto LAB:DIRECTORIO ...]
                          [--ventana N]
                          [--salida DIRECTORIO]
                          [--debug]

Ejemplos:
  python3 calcula_diff.py \\
      --roa /var/log/satres/448/itu/twroa61.140 \\
      --remoto PTB05:/datos/ptb \\
      --remoto SP01:/datos/sp \\
      --ventana 5 \\
      --salida /var/log/satres/diff
"""

import os
import sys
import re
import argparse
import logging
from typing import Optional

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import plotly.graph_objects as go

# ---------------------------------------------------------------------------
# Versión
# ---------------------------------------------------------------------------
__version__ = "1.0"

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
COLS_ITU = ["LOC", "REM", "LI", "MJD", "STTIME", "NTL",
            "TW", "DRMS", "SMP", "ATL", "REFDELAY", "RSIG",
            "CI", "S", "CALR", "ESDVAR", "ESIG", "TMP", "HUM", "PRES"]

# ---------------------------------------------------------------------------
# Utilidades de nomenclatura
# ---------------------------------------------------------------------------

def mjd_a_partes(mjd: int) -> tuple:
    """Convierte MJD entero a (MM, MMM) para el nombre de fichero."""
    mjd_str = f"{mjd:05d}"
    return mjd_str[-5:-3], mjd_str[-3:]


def nombre_itu_roa(lab: str, mjd: int) -> str:
    """Nombre del fichero ITU de ROA: twroa<MM>.<MMM>"""
    mm, mmm = mjd_a_partes(mjd)
    letras = re.sub(r'\d', '', lab).lower()
    return f"tw{letras}{mm}.{mmm}"


def buscar_fichero_itu(directorio: str, lab: str, mjd: int) -> Optional[str]:
    """
    Busca el fichero ITU de un laboratorio remoto en el directorio indicado.
    El nombre sigue el patrón: tw<letras_lab><MM>.<MMM>
    Insensible a mayúsculas/minúsculas.
    Devuelve la ruta completa o None si no se encuentra.
    """
    mm, mmm = mjd_a_partes(mjd)
    letras = re.sub(r'\d', '', lab).lower()
    nombre_base = f"tw{letras}{mm}.{mmm}"

    if not os.path.isdir(directorio):
        return None

    for f in os.listdir(directorio):
        if f.lower() == nombre_base.lower():
            return os.path.join(directorio, f)
    return None


# ---------------------------------------------------------------------------
# Lectura de fichero ITU → DataFrame
# ---------------------------------------------------------------------------

def leer_itu(ruta: str) -> pd.DataFrame:
    """Lee un fichero ITU y devuelve un DataFrame."""
    if not ruta or not os.path.isfile(ruta):
        return pd.DataFrame(columns=COLS_ITU)
    try:
        df = pd.read_table(
            ruta,
            sep=r'\s+',
            comment='*',
            names=COLS_ITU,
            engine='python',
        )
        # Normalizar STTIME y MJD
        df['_mjd']    = df['MJD'].astype(int)
        df['_sttime'] = df['STTIME'].astype(str).str.strip().str.zfill(6)
        logging.debug("ITU leído: %s (%d slots)", ruta, len(df))
        return df
    except Exception as e:
        logging.error("Error leyendo %s: %s", ruta, e)
        return pd.DataFrame(columns=COLS_ITU)


# ---------------------------------------------------------------------------
# Cálculo de diferencias para un par ROA-lab en un día
# ---------------------------------------------------------------------------

def calcular_par_dia(dfroa: pd.DataFrame, dfrem: pd.DataFrame,
                     rem_en_roa: str, loc_roa: str) -> pd.DataFrame:
    """
    Calcula UTC(ROA) - UTC(rem) para todos los slots coincidentes.
    Devuelve DataFrame con columnas: mjd, sttime, diff_ns, tw_a, tw_b,
    smp_a, smp_b, drms_a, drms_b, djm
    """
    df1 = dfroa[dfroa['REM'] == rem_en_roa].copy()
    df2 = dfrem[dfrem['REM'] == loc_roa].copy()

    if df1.empty or df2.empty:
        return pd.DataFrame()

    df = pd.merge(df1, df2, on=['_mjd', '_sttime'], suffixes=('_a', '_b'))
    if df.empty:
        return pd.DataFrame()

    # Detectar inconsistencias en S
    mask_inc = df['S_a'] != df['S_b']
    if mask_inc.any():
        for _, r in df[mask_inc].iterrows():
            logging.warning(
                "Slot %s %s: S_A=%d S_B=%d — descartado",
                r['_mjd'], r['_sttime'], int(r['S_a']), int(r['S_b'])
            )
        df = df[~mask_inc].copy()

    if df.empty:
        return pd.DataFrame()

    # Conversiones a nanosegundos
    tw_a  = df['TW_a']       * 1e9
    tw_b  = df['TW_b']       * 1e9
    ref_a = df['REFDELAY_a'] * 1e9
    ref_b = df['REFDELAY_b'] * 1e9

    esdvar_a = df['ESDVAR_a'].where(df['S_a'] == 1, other=0.0)
    esdvar_b = df['ESDVAR_b'].where(df['S_b'] == 1, other=0.0)
    calr_a   = df['CALR_a'].where(df['S_a'] == 1, other=0.0)
    calr_b   = df['CALR_b'].where(df['S_b'] == 1, other=0.0)

    # Ecuación ITU vectorizada
    diff_ns = ( 0.5 * (tw_a + esdvar_a) + ref_a
              - 0.5 * (tw_b + esdvar_b) - ref_b
              + 0.5 * (calr_a - calr_b) )

    # DJM fraccionario para el eje X
    st = df['_sttime']
    hh = st.str[:2].astype(int)
    mm = st.str[2:4].astype(int)
    ss = st.str[4:6].astype(int)
    minutos = hh * 60 + mm + ss / 60.0
    djm = df['_mjd'] + minutos / 1440.0

    resultado = pd.DataFrame({
        'mjd':    df['_mjd'].values,
        'sttime': df['_sttime'].values,
        'diff_ns': diff_ns.values,
        'tw_a':   (df['TW_a'] * 1e9).values,
        'tw_b':   (df['TW_b'] * 1e9).values,
        'smp_a':  df['SMP_a'].values,
        'smp_b':  df['SMP_b'].values,
        'drms_a': df['DRMS_a'].values,
        'drms_b': df['DRMS_b'].values,
        'djm':    djm.values,
    })
    return resultado.sort_values('djm').reset_index(drop=True)


# ---------------------------------------------------------------------------
# Escritura del fichero .dat
# ---------------------------------------------------------------------------

def escribir_dat(df: pd.DataFrame, lab_a: str, lab_b: str,
                 mjd: int, dir_salida: str) -> str:
    """Escribe el fichero .dat de diferencias y devuelve la ruta."""
    os.makedirs(dir_salida, exist_ok=True)
    nombre  = f"diff_{lab_a.lower()}_{lab_b.lower()}_{mjd:05d}.dat"
    ruta    = os.path.join(dir_salida, nombre)

    media = df['diff_ns'].mean()
    sigma = df['diff_ns'].std(ddof=1) if len(df) > 1 else 0.0

    with open(ruta, "w", encoding="utf-8") as f:
        f.write(f"* {nombre}\n")
        f.write(f"* UTC({lab_a}) - UTC({lab_b})\n")
        f.write(f"* MJD: {mjd}   Slots: {len(df)}   "
                f"Media: {media:+.3f} ns   sigma: {sigma:.3f} ns\n")
        f.write("*\n")
        f.write(f"* {'MJD':>5} {'STTIME':>6}  {'DIFF_NS':>10}  "
                f"{'TW_A_NS':>16}  {'TW_B_NS':>16}  "
                f"{'SMP_A':>5} {'SMP_B':>5}  "
                f"{'DRMS_A':>6} {'DRMS_B':>6}\n")
        f.write(f"* {'':>5} {'hhmmss':>6}  {'ns':>10}  "
                f"{'ns':>16}  {'ns':>16}  "
                f"{'':>5} {'':>5}  "
                f"{'ns':>6} {'ns':>6}\n")
        for _, r in df.iterrows():
            f.write(
                f"  {int(r['mjd']):5d} {r['sttime']:6s}  {r['diff_ns']:+10.3f}  "
                f"{r['tw_a']:+16.3f}  {r['tw_b']:+16.3f}  "
                f"{int(r['smp_a']):5d} {int(r['smp_b']):5d}  "
                f"{r['drms_a']:6.3f} {r['drms_b']:6.3f}\n"
            )
    return ruta


# ---------------------------------------------------------------------------
# Visualización
# ---------------------------------------------------------------------------

def generar_graficas(series: dict, lab_local: str,
                     dir_salida: str, mjd_ini: int, ventana: int) -> None:
    """
    Genera gráfica HTML interactiva (plotly) y PNG (matplotlib)
    con todos los laboratorios en la misma figura.

    series: dict lab_remoto → DataFrame con columnas djm y diff_ns
    """
    if not series:
        logging.warning("Sin datos para generar gráficas.")
        return

    titulo = f"UTC({lab_local}) - UTC(lab)   MJD {mjd_ini-ventana+1}–{mjd_ini}"
    nombre_base = os.path.join(
        dir_salida,
        f"diff_{lab_local.lower()}_{mjd_ini:05d}_v{ventana}"
    )

    # --- Plotly (HTML interactivo) ---
    fig_html = go.Figure()
    for lab, df in sorted(series.items()):
        if df.empty:
            continue
        fig_html.add_trace(go.Scatter(
            x=df['djm'],
            y=df['diff_ns'],
            mode='lines+markers',
            name=f"UTC({lab_local})-UTC({lab})",
            marker=dict(size=5),
            hovertemplate=(
                f"<b>UTC({lab_local})-UTC({lab})</b><br>"
                "DJM: %{x:.6f}<br>"
                "Diff: %{y:.3f} ns<br>"
                "<extra></extra>"
            ),
        ))

    fig_html.update_layout(
        title=titulo,
        xaxis_title="DJM",
        yaxis_title="ns",
        legend_title="Par",
        hovermode="x unified",
        template="plotly_white",
        height=500,
    )
    ruta_html = nombre_base + ".html"
    fig_html.write_html(ruta_html, include_plotlyjs='cdn')
    logging.info("Gráfica HTML generada: %s", ruta_html)

    # --- Matplotlib (PNG para informes) ---
    fig, ax = plt.subplots(figsize=(15, 5))
    for lab, df in sorted(series.items()):
        if df.empty:
            continue
        media = df['diff_ns'].mean()
        ax.plot(df['djm'], df['diff_ns'], 'o-', markersize=4,
                label=(f"UTC({lab_local})-UTC({lab})  "                        f"σ={df['diff_ns'].std(ddof=1):.3f} ns"))

    ax.set_xlabel("DJM")
    ax.set_ylabel("ns")
    ax.set_title(titulo)
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)
    ax.ticklabel_format(useOffset=False)
    fig.tight_layout()

    ruta_png = nombre_base + ".png"
    fig.savefig(ruta_png, dpi=150)
    plt.close(fig)
    logging.info("Gráfica PNG generada: %s", ruta_png)


# ---------------------------------------------------------------------------
# Proceso principal
# ---------------------------------------------------------------------------

def procesar(ruta_local_base: str, remotos: list,
             ventana: int, dir_salida: str) -> None:
    """
    ruta_local_base : ruta al fichero ITU de ROA del día más reciente
    remotos       : lista de (lab_nombre, directorio)
    ventana       : número de días a procesar
    dir_salida    : directorio para .dat y gráficas
    """
    # Extraer MJD del nombre del fichero ROA
    basename = os.path.basename(ruta_local_base)
    m = re.search(r'(\d{2})\.(\d{3})$', basename)
    if not m:
        logging.error("No se puede extraer el MJD del fichero: %s", basename)
        sys.exit(1)

    mjd_ini = int(m.group(1) + m.group(2))
    # Reconstruir MJD completo (5 cifras) desde las últimas 5
    # Estimamos la centena desde el MJD actual aproximado
    mjd_ref = 61000  # base aproximada para los MJD actuales
    while (mjd_ref % 100000) % 1000 != mjd_ini % 1000:
        mjd_ref += 1
    # Mejor: extraer del contenido del fichero
    df_test = leer_itu(ruta_local_base)
    if df_test.empty:
        logging.error("No se puede leer el fichero ROA: %s", ruta_local_base)
        sys.exit(1)
    mjd_ini  = int(df_test['MJD'].iloc[0])
    lab_local = df_test['LOC'].iloc[0]

    logging.info("Laboratorio local : %s", lab_local)
    logging.info("MJD inicial: %d  Ventana: %d días", mjd_ini, ventana)

    # Acumular series para la gráfica
    series_total: dict = {lab: pd.DataFrame() for lab, _ in remotos}

    # Procesar cada día de la ventana
    for i in range(ventana):
        mjd = mjd_ini - i
        logging.info("─── MJD %d ───", mjd)

        # Leer fichero ROA del día (búsqueda case-insensitive)
        dir_roa = os.path.dirname(ruta_local_base)
        ruta_roa = buscar_fichero_itu(dir_roa, lab_local, mjd)
        if ruta_roa is None:
            logging.warning("ROA MJD %d: fichero no encontrado en %s", mjd, dir_roa)
            continue

        dfroa = leer_itu(ruta_roa)
        if dfroa.empty:
            continue

        # Procesar cada laboratorio remoto
        for lab_rem, dir_rem in remotos:
            ruta_rem = buscar_fichero_itu(dir_rem, lab_rem, mjd)
            if ruta_rem is None:
                logging.warning("%s MJD %d: fichero no encontrado en %s",
                                lab_rem, mjd, dir_rem)
                continue

            dfrem = leer_itu(ruta_rem)
            if dfrem.empty:
                continue

            # Detectar LOC real del remoto
            loc_rem = dfrem['LOC'].iloc[0] if not dfrem.empty else lab_rem

            df_diff = calcular_par_dia(dfroa, dfrem, loc_rem, lab_local)
            if df_diff.empty:
                logging.warning("%s MJD %d: sin slots coincidentes", lab_rem, mjd)
                continue

            n = len(df_diff)
            media = df_diff['diff_ns'].mean()
            sigma = df_diff['diff_ns'].std(ddof=1) if n > 1 else 0.0
            logging.info("  %s: %d slots  Media=%+.3f ns  σ=%.3f ns",
                         lab_rem, n, media, sigma)

            # Imprimir tabla
            print(f"\n  UTC({lab_local})-UTC({lab_rem})  MJD {mjd}  {n} slots")
            print(f"  {'STTIME':>8}  {'DIFF_NS':>10}  {'SMP_A':>5} {'SMP_B':>5}  S")
            print("  " + "-" * 43)
            for _, r in df_diff.iterrows():
                print(f"  {r['sttime']:>8}  {r['diff_ns']:+10.3f}  "
                      f"{int(r['smp_a']):5d} {int(r['smp_b']):5d}")
            print("  " + "-" * 43)
            print(f"  {'Media':>8}  {media:+10.3f}  σ={sigma:.3f} ns")

            # Escribir .dat
            ruta_dat = escribir_dat(df_diff, lab_local, lab_rem, mjd, dir_salida)
            logging.info("  .dat: %s", ruta_dat)

            # Acumular para la gráfica
            series_total[lab_rem] = pd.concat(
                [series_total[lab_rem], df_diff], ignore_index=True
            )

    # Ordenar cada serie cronológicamente antes de graficar
    for lab in series_total:
        if not series_total[lab].empty:
            series_total[lab] = series_total[lab].sort_values('djm').reset_index(drop=True)

    # Generar gráficas con todos los labs y todos los días
    generar_graficas(series_total, lab_local, dir_salida, mjd_ini, ventana)


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="calcula_diff.py — Diferencias de tiempo TWSTFT"
    )
    parser.add_argument(
        "--local", type=str, required=True,
        help="Fichero ITU del laboratorio local del día más reciente (p.ej. twroa61.140)"
    )
    parser.add_argument(
        "--remoto", type=str, action="append", default=[],
        metavar="LAB:DIRECTORIO",
        help="Laboratorio remoto y directorio de sus ficheros ITU. "
             "Repetir para varios labs (p.ej. --remoto PTB05:/datos/ptb)"
    )
    parser.add_argument(
        "--ventana", type=int, default=1,
        help="Número de días a procesar (defecto: 1)"
    )
    parser.add_argument(
        "--salida", type=str, default=".",
        help="Directorio de salida para .dat y gráficas (defecto: actual)"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Log detallado en consola"
    )
    parser.add_argument(
        "--version", action="version",
        version=f"calcula_diff.py {__version__}"
    )
    args = parser.parse_args()

    nivel = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=nivel,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )

    # Parsear --remoto LAB:DIRECTORIO
    remotos = []
    for r in args.remoto:
        partes = r.split(':', 1)
        if len(partes) != 2:
            print(f"Error: --remoto debe tener formato LAB:DIRECTORIO, recibido: {r}",
                  file=sys.stderr)
            sys.exit(1)
        remotos.append((partes[0].strip(), partes[1].strip()))

    if not remotos:
        print("Error: se necesita al menos un --remoto LAB:DIRECTORIO",
              file=sys.stderr)
        sys.exit(1)

    if not os.path.isfile(args.local):
        print(f"Error: fichero ROA no encontrado: {args.local}", file=sys.stderr)
        sys.exit(1)

    logging.info("=" * 60)
    logging.info("calcula_diff.py v%s", __version__)
    logging.info("Local   : %s", args.local)
    logging.info("Ventana : %d días", args.ventana)
    for lab, d in remotos:
        logging.info("Remoto  : %s → %s", lab, d)
    logging.info("Salida  : %s", args.salida)
    logging.info("=" * 60)

    procesar(args.local, remotos, args.ventana, args.salida)


if __name__ == "__main__":
    main()
