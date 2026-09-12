#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
envia_zabbix.py
===============
Calcula las diferencias de tiempo UTC(local) - UTC(lab_remoto) entre el
laboratorio local y uno o varios laboratorios remotos, a partir de sus
ficheros ITU (Anexo 2, Recomendación UIT-R TF.1153-4), y envía los
resultados a un servidor Zabbix via zabbix_sender.

Cada slot se envía como un item independiente con su timestamp real,
lo que permite a Zabbix y Grafana representar la serie temporal completa
y detectar valores fuera de norma.

Ecuación (UIT-R TF.1153-4, sección 8.2):

  S=1 (calibrado):
    UTC(1)-UTC(2) = +0.5·[TW(1)+ESDVAR(1)] + REFDELAY(1)
                    -0.5·[TW(2)+ESDVAR(2)] - REFDELAY(2)
                    +0.5·[CALR(1,2) - CALR(2,1)]

  S=9 (no calibrado):
    UTC(1)-UTC(2)+K = +0.5·[TW(1)+ESDVAR(1)] + REFDELAY(1)
                      -0.5·[TW(2)+ESDVAR(2)] - REFDELAY(2)

  S_A != S_B → slot descartado con aviso.

Configuración Zabbix en twstft.ini:
  [zabbix]
  servidor    = zabbix.roa.es
  puerto      = 10051
  host        = TWSTFT
  item_prefix = twstft
  activo      = si

Item key generado: <prefix>.<loc_lower>.<rem_lower>
  Ejemplo: twstft.roa01.ptb05

Uso:
  python3 envia_zabbix.py --local FICHERO_ITU_LOCAL
                          --remoto LAB[:DIRECTORIO] [--remoto ...]
                          [--ventana N]
                          [--config RUTA_INI]
                          [--debug]

Ejemplos:
  python3 envia_zabbix.py \\
      --local  /home/tw/satres/448/itu/twroa61.199 \\
      --remoto PTB05:/home/tw/satres/itu_remoto/ptb \\
      --remoto SP01 \\
      --ventana 1 \\
      --config  /home/tw/twstft/twstft.ini
"""

import os
import sys
import re
import argparse
import logging
import configparser
import ftplib
import urllib.request
import urllib.parse
import subprocess
import tempfile
import datetime
from typing import Optional

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Versión
# ---------------------------------------------------------------------------
__version__ = "0.1"

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
COLS_ITU = ["LOC", "REM", "LI", "MJD", "STTIME", "NTL",
            "TW", "DRMS", "SMP", "ATL", "REFDELAY", "RSIG",
            "CI", "S", "CALR", "ESDVAR", "ESIG", "TMP", "HUM", "PRES"]

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

def cargar_config(ruta: str) -> Optional[configparser.ConfigParser]:
    if not ruta or not os.path.isfile(ruta):
        return None
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


def get_zabbix_cfg(cfg: Optional[configparser.ConfigParser]) -> dict:
    """Lee la configuración de Zabbix desde twstft.ini."""
    if cfg is None:
        return {}
    return {
        'servidor':    get_str(cfg, 'zabbix', 'servidor',    ''),
        'puerto':      get_str(cfg, 'zabbix', 'puerto',      '10051'),
        'host':        get_str(cfg, 'zabbix', 'host',        'TWSTFT'),
        'item_prefix': get_str(cfg, 'zabbix', 'item_prefix', 'twstft'),
        'activo':      get_str(cfg, 'zabbix', 'activo',      'no').lower()
                       in ('si', 'sí', 'yes'),
    }


def get_lab_cfg(cfg: Optional[configparser.ConfigParser], lab: str) -> dict:
    """Devuelve los parámetros de descarga de un laboratorio desde twstft.ini."""
    if cfg is None:
        return {}
    for sec in cfg.sections():
        if not sec.lower().startswith("lab "):
            continue
        nombre = cfg.get(sec, "nombre", fallback="").strip()
        if nombre.upper() == lab.upper():
            return {
                "itu_url":   cfg.get(sec, "itu_url",   fallback="").strip(),
                "itu_mayus": cfg.get(sec, "itu_mayus", fallback="no").strip().lower()
                             in ('si', 'sí', 'yes'),
            }
    return {}


# ---------------------------------------------------------------------------
# Nomenclatura de ficheros ITU
# ---------------------------------------------------------------------------

def mjd_a_partes(mjd: int) -> tuple:
    mjd_str = f"{mjd:05d}"
    return mjd_str[-5:-3], mjd_str[-3:]


def nombre_itu(lab: str, mjd: int) -> str:
    mm, mmm = mjd_a_partes(mjd)
    letras = re.sub(r'\d', '', lab).lower()
    return f"tw{letras}{mm}.{mmm}"


def buscar_fichero_itu(directorio: str, lab: str, mjd: int) -> Optional[str]:
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
# Descarga FTP/HTTP
# ---------------------------------------------------------------------------

def descargar_itu(url_base: str, nombre_fichero: str,
                  destino: str, mayus: bool) -> Optional[str]:
    nombre = nombre_fichero.upper() if mayus else nombre_fichero.lower()
    os.makedirs(destino, exist_ok=True)
    ruta_local = os.path.join(destino, nombre)
    parsed = urllib.parse.urlparse(url_base)
    try:
        if parsed.scheme == "ftp":
            host     = parsed.hostname
            usuario  = urllib.parse.unquote(parsed.username or "anonymous")
            password = urllib.parse.unquote(parsed.password or "")
            directorio = parsed.path
            logging.info("FTP: %s@%s%s/%s", usuario, host, directorio, nombre)
            ftp = ftplib.FTP(host, timeout=30)  # type: ignore[arg-type]
            ftp.login(usuario, password)
            if directorio:
                ftp.cwd(directorio)
            with open(ruta_local, "wb") as f:
                ftp.retrbinary(f"RETR {nombre}", f.write)
            ftp.quit()
        elif parsed.scheme in ("http", "https"):
            url = f"{url_base.rstrip('/')}/{nombre}"
            logging.info("HTTP: %s", url)
            urllib.request.urlretrieve(url, ruta_local)
        else:
            logging.error("Esquema no soportado: %s", parsed.scheme)
            return None
        logging.info("Descargado: %s → %s", nombre, ruta_local)
        return ruta_local
    except Exception as e:
        logging.error("Error descargando %s: %s", nombre, e)
        if os.path.isfile(ruta_local):
            os.remove(ruta_local)
        return None


# ---------------------------------------------------------------------------
# Lectura de fichero ITU
# ---------------------------------------------------------------------------

def leer_itu(ruta: str) -> pd.DataFrame:
    if not ruta or not os.path.isfile(ruta):
        return pd.DataFrame(columns=COLS_ITU)
    try:
        df = pd.read_table(
            ruta, sep=r'\s+', comment='*',
            names=COLS_ITU, engine='python',
        )
        df['_mjd']    = df['MJD'].astype(int)
        df['_sttime'] = df['STTIME'].astype(str).str.strip().str.zfill(6)
        logging.debug("ITU leído: %s (%d slots)", ruta, len(df))
        return df
    except Exception as e:
        logging.error("Error leyendo %s: %s", ruta, e)
        return pd.DataFrame(columns=COLS_ITU)


# ---------------------------------------------------------------------------
# Cálculo de diferencias
# ---------------------------------------------------------------------------

def calcular_par_dia(dfroa: pd.DataFrame, dfrem: pd.DataFrame,
                     rem_en_local: str, loc_local: str) -> pd.DataFrame:
    df1 = dfroa[dfroa['REM'] == rem_en_local].copy()
    df2 = dfrem[dfrem['REM'] == loc_local].copy()
    if df1.empty or df2.empty:
        return pd.DataFrame()

    df = pd.merge(df1, df2, on=['_mjd', '_sttime'], suffixes=('_a', '_b'))
    if df.empty:
        return pd.DataFrame()

    # Detectar inconsistencias en S
    mask_inc = df['S_a'] != df['S_b']
    if mask_inc.any():
        for _, r in df[mask_inc].iterrows():
            logging.warning("Slot %s %s: S_A=%d S_B=%d — descartado",
                            r['_mjd'], r['_sttime'], int(r['S_a']), int(r['S_b']))
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

    # Ecuación UIT-R TF.1153-4 §8.2
    diff_ns = ( 0.5 * (tw_a + esdvar_a) + ref_a
              - 0.5 * (tw_b + esdvar_b) - ref_b
              + 0.5 * (calr_a - calr_b) )

    return pd.DataFrame({
        'mjd':     df['_mjd'].values,
        'sttime':  df['_sttime'].values,
        'diff_ns': diff_ns.values,
        'smp_a':   df['SMP_a'].values,
        'smp_b':   df['SMP_b'].values,
    }).sort_values('sttime').reset_index(drop=True)


# ---------------------------------------------------------------------------
# Envío a Zabbix
# ---------------------------------------------------------------------------

def mjd_sttime_a_timestamp(mjd: int, sttime: str) -> int:
    """Convierte MJD + STTIME (HHMMSS) a timestamp Unix."""
    hh = int(sttime[:2]); mm = int(sttime[2:4]); ss = int(sttime[4:6])
    fecha = datetime.datetime(1858, 11, 17) + datetime.timedelta(days=mjd)
    dt = fecha.replace(hour=hh, minute=mm, second=ss)
    epoch = datetime.datetime(1970, 1, 1)
    return int((dt - epoch).total_seconds())


def enviar_a_zabbix(df: pd.DataFrame, lab_local: str, lab_remoto: str,
                    zcfg: dict) -> bool:
    """
    Envía los valores diff_ns de cada slot a Zabbix via zabbix_sender.
    Item key: <prefix>.<loc_lower>.<rem_lower>
    Cada slot se envía con su timestamp real.
    """
    if not zcfg.get('servidor'):
        logging.error("Zabbix: servidor no configurado en [zabbix] de twstft.ini")
        return False

    servidor    = zcfg['servidor']
    puerto      = zcfg['puerto']
    host        = zcfg['host']
    item_key    = (f"{zcfg['item_prefix']}."
                   f"{lab_local.lower()}.{lab_remoto.lower()}")

    lineas = []
    for _, r in df.iterrows():
        ts    = mjd_sttime_a_timestamp(int(r['mjd']), str(r['sttime']))
        valor = f"{r['diff_ns']:.6f}"
        lineas.append(f"{host} {item_key} {ts} {valor}")

    if not lineas:
        return False

    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.zbx',
                                         delete=False, encoding='utf-8') as f:
            f.write('\n'.join(lineas) + '\n')
            ruta_tmp = f.name

        cmd = ['zabbix_sender', '-z', servidor, '-p', puerto, '-i', ruta_tmp]
        resultado = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        os.unlink(ruta_tmp)

        if resultado.returncode == 0:
            logging.info("Zabbix: %d slots enviados → %s [%s:%s]",
                         len(lineas), item_key, servidor, puerto)
            logging.debug("zabbix_sender: %s", resultado.stdout.strip())
            return True
        else:
            logging.error("zabbix_sender error (rc=%d): %s",
                          resultado.returncode, resultado.stderr.strip())
            return False

    except FileNotFoundError:
        logging.error("Zabbix: zabbix_sender no encontrado. "
                      "Instalar con: sudo dnf install zabbix-sender")
        return False
    except subprocess.TimeoutExpired:
        logging.error("Zabbix: timeout conectando con %s", servidor)
        return False
    except Exception as e:
        logging.error("Zabbix: error inesperado: %s", e)
        return False


# ---------------------------------------------------------------------------
# Proceso principal
# ---------------------------------------------------------------------------

def procesar(ruta_local_base: str, remotos: list,
             ventana: int, cfg: Optional[configparser.ConfigParser]) -> None:

    # Leer MJD y lab local del fichero ITU
    df_test = leer_itu(ruta_local_base)
    if df_test.empty:
        logging.error("No se puede leer el fichero local: %s", ruta_local_base)
        sys.exit(1)
    mjd_ini   = int(df_test['MJD'].iloc[0])
    lab_local = df_test['LOC'].iloc[0]

    logging.info("Laboratorio local : %s", lab_local)
    logging.info("MJD inicial: %d  Ventana: %d días", mjd_ini, ventana)

    # Configuración Zabbix
    zcfg = get_zabbix_cfg(cfg)
    if not zcfg.get('activo', False):
        logging.warning("Zabbix no está activado en twstft.ini ([zabbix] activo = si). "
                        "Activar para enviar datos.")
    else:
        logging.info("Zabbix: %s:%s  host=%s  prefix=%s",
                     zcfg['servidor'], zcfg['puerto'],
                     zcfg['host'], zcfg['item_prefix'])

    dir_local = os.path.dirname(ruta_local_base)
    total_enviados = 0

    for i in range(ventana):
        mjd = mjd_ini - i
        logging.info("─── MJD %d ───", mjd)

        # Fichero ITU local (búsqueda case-insensitive)
        ruta_local = buscar_fichero_itu(dir_local, lab_local, mjd)
        if ruta_local is None:
            logging.warning("Local MJD %d: no encontrado en %s", mjd, dir_local)
            continue
        df_local = leer_itu(ruta_local)
        if df_local.empty:
            continue

        for lab_rem, dir_rem in remotos:
            ruta_rem = None

            # Buscar en directorio local
            if dir_rem:
                ruta_rem = buscar_fichero_itu(dir_rem, lab_rem, mjd)

            # Si no está, intentar descarga automática via ini
            if ruta_rem is None and cfg is not None:
                lab_cfg  = get_lab_cfg(cfg, lab_rem)
                itu_url  = lab_cfg.get("itu_url", "")
                itu_mayus = lab_cfg.get("itu_mayus", False)
                if itu_url:
                    dir_cache = dir_rem if dir_rem else os.path.join(
                        os.path.dirname(ruta_local_base), "cache", lab_rem.lower())
                    ruta_rem = descargar_itu(itu_url, nombre_itu(lab_rem, mjd),
                                             dir_cache, itu_mayus)

            if ruta_rem is None:
                logging.warning("%s MJD %d: fichero no encontrado ni descargado",
                                lab_rem, mjd)
                continue

            df_rem = leer_itu(ruta_rem)
            if df_rem.empty:
                continue

            loc_rem = df_rem['LOC'].iloc[0]
            df_diff = calcular_par_dia(df_local, df_rem, loc_rem, lab_local)

            if df_diff.empty:
                logging.warning("%s MJD %d: sin slots coincidentes", lab_rem, mjd)
                continue

            n     = len(df_diff)
            media = df_diff['diff_ns'].mean()
            sigma = df_diff['diff_ns'].std(ddof=1) if n > 1 else 0.0
            logging.info("  %s: %d slots  Media=%+.3f ns  σ=%.3f ns",
                         lab_rem, n, media, sigma)

            # Enviar a Zabbix
            if zcfg.get('activo', False):
                ok = enviar_a_zabbix(df_diff, lab_local, lab_rem, zcfg)
                if ok:
                    total_enviados += n

    if zcfg.get('activo', False):
        logging.info("Total slots enviados a Zabbix: %d", total_enviados)


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------

def main() -> None:
    script_dir     = os.path.dirname(os.path.abspath(__file__))
    config_default = os.path.join(script_dir, "twstft.ini")

    parser = argparse.ArgumentParser(
        description="envia_zabbix.py — Envía diferencias de tiempo TWSTFT a Zabbix"
    )
    parser.add_argument(
        "--local", type=str, required=True,
        help="Fichero ITU del laboratorio local del día más reciente"
    )
    parser.add_argument(
        "--remoto", type=str, action="append", default=[],
        metavar="LAB[:DIRECTORIO]",
        help="Laboratorio remoto y (opcionalmente) directorio de sus ficheros ITU. "
             "Repetir para varios labs. Si se omite el directorio se usa itu_url del ini."
    )
    parser.add_argument(
        "--ventana", type=int, default=1,
        help="Número de días a procesar hacia atrás (defecto: 1)"
    )
    parser.add_argument(
        "--config", type=str, default=config_default,
        help=f"Ruta a twstft.ini (defecto: {config_default})"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Log detallado en consola"
    )
    parser.add_argument(
        "--version", action="version",
        version=f"envia_zabbix.py {__version__}"
    )
    args = parser.parse_args()

    nivel = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=nivel,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )

    # Parsear --remoto LAB[:DIRECTORIO]
    remotos = []
    for r in args.remoto:
        partes = r.split(':', 1)
        lab = partes[0].strip()
        directorio = partes[1].strip() if len(partes) == 2 else ""
        remotos.append((lab, directorio))

    if not remotos:
        print("Error: se necesita al menos un --remoto LAB o --remoto LAB:DIRECTORIO",
              file=sys.stderr)
        sys.exit(1)

    if not os.path.isfile(args.local):
        print(f"Error: fichero local no encontrado: {args.local}", file=sys.stderr)
        sys.exit(1)

    cfg = cargar_config(args.config)

    logging.info("=" * 60)
    logging.info("envia_zabbix.py v%s", __version__)
    logging.info("Local   : %s", args.local)
    logging.info("Ventana : %d días", args.ventana)
    logging.info("Config  : %s", args.config)
    for lab, d in remotos:
        logging.info("Remoto  : %s → %s", lab, d if d else "(descarga automática)")
    logging.info("=" * 60)

    procesar(args.local, remotos, args.ventana, cfg)


if __name__ == "__main__":
    main()
