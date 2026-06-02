#!/usr/bin/env python3
"""
lee_satres.py
=============
Escucha los mensajes UDP del módem SATRE en el puerto especificado en
twstft.ini y los almacena en ficheros RAW diarios con rotación automática.

Modos de ejecución:
    Normal (daemon)  — silencioso, log a fichero con rotación diaria
    Debug            — log a consola con nivel DEBUG, sin daemonizar

Fichero de configuración: twstft.ini (por defecto en el mismo directorio
que este script). Se puede especificar otra ruta con --config.

Secciones relevantes de twstft.ini:
    [network]  puerto   = puerto UDP de escucha
    [ficheros] base     = directorio raíz para los ficheros raw

Nomenclatura de ficheros raw:
    <base>/<unit_id>/raw.YYYYMMDD

Nomenclatura de ficheros de log:
    <base>/log/lee_satres.YYYYMMDD.log

Uso:
    python3 lee_satres.py [--config RUTA_INI] [--debug]
"""

import socket
import os
import sys
import re
import argparse
import logging
import logging.handlers
import configparser
from datetime import datetime

# ---------------------------------------------------------------------------
# Versión
# ---------------------------------------------------------------------------
__version__ = "0.3"

# ---------------------------------------------------------------------------
# Constantes por defecto
# ---------------------------------------------------------------------------
PUERTO_DEFAULT  = 3020
BASEDIR_DEFAULT = "/var/log/satres"
ENCODING        = "ascii"
BUFFER_SIZE     = 4096
CONFIG_FILENAME = "twstft.ini"

# ---------------------------------------------------------------------------
# Patrones de reconocimiento de mensajes SATRE
# ---------------------------------------------------------------------------

# %0>YY/MM/DD;HH:MM:SS;unit_id;...
RE_MSG0 = re.compile(
    r'^%0>'
    r'(\d{2}/\d{2}/\d{2})'
    r';(\d{2}:\d{2}:\d{2})'
    r';(\d{1,5})'
)

# %Tx >  /  %Rx1>  /  %Rx2>  /  %Rx3>   YYYY/MM/DD;HH:MM:SS;unit_id;...
RE_MSGPERIODIC = re.compile(
    r'^%(?:Tx\s*>|Rx\d>)'
    r'(\d{4}/\d{2}/\d{2})'
    r';(\d{2}:\d{2}:\d{2})'
    r';(\d{1,5})'
)

# ---------------------------------------------------------------------------
# Configuración del logging
# ---------------------------------------------------------------------------

def configurar_logging(basedir: str, debug: bool) -> None:
    """
    Debug:  StreamHandler a stdout, nivel DEBUG.
    Daemon: TimedRotatingFileHandler a <basedir>/log/lee_satres.YYYY-MM-DD.log,
            nivel INFO, rotación a medianoche, 30 días de retención.
    """
    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    logger = logging.getLogger()
    logger.handlers.clear()

    if debug:
        logger.setLevel(logging.DEBUG)
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    else:
        logger.setLevel(logging.INFO)
        log_dir = os.path.join(basedir, "log")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "lee_satres.log")
        # Rotación diaria a medianoche, sufijo de fecha, 30 días de retención
        handler = logging.handlers.TimedRotatingFileHandler(
            filename=log_path,
            when="midnight",
            interval=1,
            backupCount=30,
            encoding="utf-8",
            utc=True,
        )
        handler.suffix = "%Y%m%d"
        handler.setFormatter(fmt)
        logger.addHandler(handler)
        # En modo daemon no escribimos nada en stdout/stderr salvo arranque
        logging.info("Logging iniciado → %s (rotación diaria UTC)", log_path)


# ---------------------------------------------------------------------------
# Lectura de configuración
# ---------------------------------------------------------------------------

def cargar_configuracion(ruta_config: str) -> configparser.ConfigParser:
    if not os.path.isfile(ruta_config):
        raise FileNotFoundError(
            f"Fichero de configuración no encontrado: {ruta_config}"
        )
    cfg = configparser.ConfigParser(
        interpolation=None,
        inline_comment_prefixes=("#",),
    )
    cfg.read(ruta_config, encoding="utf-8")
    return cfg


def obtener_puerto(cfg: configparser.ConfigParser) -> int:
    try:
        return cfg.getint("network", "puerto")
    except (configparser.NoSectionError, configparser.NoOptionError):
        logging.warning(
            "No se encontró [network]/puerto en twstft.ini, usando %d",
            PUERTO_DEFAULT,
        )
        return PUERTO_DEFAULT


def obtener_basedir(cfg: configparser.ConfigParser) -> str:
    try:
        return cfg.get("ficheros", "base").strip()
    except (configparser.NoSectionError, configparser.NoOptionError):
        logging.warning(
            "No se encontró [ficheros]/base en twstft.ini, usando %s",
            BASEDIR_DEFAULT,
        )
        return BASEDIR_DEFAULT


# ---------------------------------------------------------------------------
# Gestión de ficheros raw por equipo
# ---------------------------------------------------------------------------

class GestorFicheros:
    """
    Mantiene un fichero raw abierto por unit_id con rotación diaria.
    Ruta: <basedir>/<unit_id>/raw.YYYYMMDD
    """

    def __init__(self, basedir: str):
        self.basedir = basedir
        # unit_id → {"fichero": <file>, "ruta": str, "fecha": str}
        self._ficheros: dict = {}

    def _abrir(self, unit_id: str, fecha_str: str):
        directorio = os.path.join(self.basedir, unit_id)
        os.makedirs(directorio, exist_ok=True)
        ruta = os.path.join(directorio, f"raw.{fecha_str}")
        f = open(ruta, "a", encoding="utf-8")
        self._ficheros[unit_id] = {"fichero": f, "ruta": ruta, "fecha": fecha_str}
        logging.info("[%s] Fichero raw abierto: %s", unit_id, ruta)
        return f

    def obtener_fichero(self, unit_id: str, fecha_str: str):
        registro = self._ficheros.get(unit_id)

        if registro is None:
            return self._abrir(unit_id, fecha_str)

        if registro["fecha"] != fecha_str:
            logging.info(
                "[%s] Rotación raw: %s → %s",
                unit_id, registro["fecha"], fecha_str,
            )
            try:
                registro["fichero"].close()
            except Exception:
                pass
            return self._abrir(unit_id, fecha_str)

        return registro["fichero"]

    def cerrar_todos(self):
        for uid, registro in self._ficheros.items():
            try:
                registro["fichero"].close()
                logging.info("[%s] Fichero raw cerrado: %s", uid, registro["ruta"])
            except Exception:
                pass
        self._ficheros.clear()


# ---------------------------------------------------------------------------
# Extracción de metadatos de un mensaje SATRE
# ---------------------------------------------------------------------------

def extraer_meta(linea: str):
    """
    Extrae (unit_id, fecha_YYYYMMDD) de una línea SATRE.
    Devuelve (None, None) si no es posible.
    """
    m = RE_MSG0.match(linea)
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%y/%m/%d")
            return m.group(3).zfill(3), dt.strftime("%Y%m%d")
        except ValueError:
            pass

    m = RE_MSGPERIODIC.match(linea)
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%Y/%m/%d")
            return m.group(3).zfill(3), dt.strftime("%Y%m%d")
        except ValueError:
            pass

    return None, None


def tipo_mensaje(linea: str) -> str:
    if linea.startswith("%0>"):    return "SISTEMA_0"
    if linea.startswith("%Tx"):    return "TX"
    if linea.startswith("%Rx1"):   return "RX1"
    if linea.startswith("%Rx2"):   return "RX2"
    if linea.startswith("%Rx3"):   return "RX3"
    if linea.startswith("$"):      return "ACK"
    if linea.startswith("*H"):     return "HIRATE"
    if linea.startswith("*9999"):  return "EOF"
    if linea.startswith("*"):      return "FILEREADER"
    if linea.startswith("~"):      return "DEBUG"
    if re.match(r'^%\d+>', linea): return "SISTEMA_N"
    return "DESCONOCIDO"


# ---------------------------------------------------------------------------
# Bucle principal
# ---------------------------------------------------------------------------

def iniciar_listener(puerto: int, basedir: str, debug: bool) -> None:
    """Escucha UDP y graba en ficheros raw por equipo y día."""

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", puerto))

    logging.info("Arrancado. Puerto UDP %d | Base dir: %s", puerto, basedir)

    gestor = GestorFicheros(basedir)

    # Estado por IP: (unit_id, fecha_str) del último %0> de cada equipo
    estado_por_ip: dict = {}

    total_rx = total_ok = total_sin_id = 0

    try:
        while True:
            # Capturar cualquier excepción dentro del bucle para que el daemon
            # no caiga silenciosamente por un error puntual (datagrama malformado,
            # error de escritura en fichero, etc.)
            try:
                datos, direccion = sock.recvfrom(BUFFER_SIZE)
                ip_origen = direccion[0]
            except OSError as exc:
                # Error de socket — loguear y continuar
                logging.error("Error recvfrom: %s", exc)
                continue

            try:
                texto = datos.decode(ENCODING, errors="replace").strip()
            except Exception:
                texto = datos.decode("utf-8", errors="replace").strip()

            if not texto:
                continue

            total_rx += 1

            try:
                unit_id, fecha_str = extraer_meta(texto)

                if unit_id and fecha_str:
                    estado_por_ip[ip_origen] = (unit_id, fecha_str)
                else:
                    if ip_origen in estado_por_ip:
                        unit_id, fecha_str = estado_por_ip[ip_origen]
                    else:
                        total_sin_id += 1
                        logging.debug("Sin ID para %s: %s", ip_origen, texto[:80])
                        continue

                f = gestor.obtener_fichero(unit_id, fecha_str)
                f.write(texto + "\n")
                f.flush()
                total_ok += 1

                tipo = tipo_mensaje(texto)
                if debug:
                    logging.debug("[%s] %-12s | %s", unit_id, tipo, texto[:120])
                else:
                    if tipo in ("SISTEMA_0",):
                        logging.info("[%s] ciclo %s", unit_id, fecha_str)

            except Exception as exc:
                # Error procesando un mensaje — loguear y continuar el bucle
                logging.error(
                    "Error procesando mensaje de %s: %s | msg: %s",
                    ip_origen, exc, texto[:80], exc_info=True,
                )

    except KeyboardInterrupt:
        logging.info(
            "Detenido. recibidos=%d grabados=%d sin_id=%d",
            total_rx, total_ok, total_sin_id,
        )
    except Exception as exc:
        # Solo llega aquí si falla algo fuera del bucle (socket, etc.)
        logging.critical("Error fatal: %s", exc, exc_info=True)
        raise
    finally:
        gestor.cerrar_todos()
        sock.close()
        logging.info("Socket cerrado.")


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------

def main() -> None:
    script_dir     = os.path.dirname(os.path.abspath(__file__))
    config_default = os.path.join(script_dir, CONFIG_FILENAME)

    parser = argparse.ArgumentParser(
        description="lee_satres.py — Receptor UDP de datos del módem SATRE"
    )
    parser.add_argument(
        "--config", type=str, default=config_default,
        help=f"Ruta al fichero de configuración (defecto: {config_default})",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Modo depuración: log detallado en consola, no daemoniza",
    )
    parser.add_argument(
        "--version", action="version",
        version=f"lee_satres.py {__version__}",
    )
    args = parser.parse_args()

    # Necesitamos basedir antes de configurar el logging, usamos un
    # logger temporal a stdout para los posibles errores de arranque
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )

    try:
        cfg = cargar_configuracion(args.config)
    except FileNotFoundError as e:
        logging.error("%s", e)
        sys.exit(1)

    puerto  = obtener_puerto(cfg)
    basedir = obtener_basedir(cfg)

    # Ahora reconfigurar el logging definitivo
    configurar_logging(basedir, args.debug)

    if args.debug:
        logging.debug("=== MODO DEBUG ===")
        logging.debug("Config  : %s", args.config)
        logging.debug("Puerto  : %d", puerto)
        logging.debug("Basedir : %s", basedir)
    else:
        logging.info(
            "lee_satres v%s arrancado | config=%s puerto=%d basedir=%s",
            __version__, args.config, puerto, basedir,
        )

    iniciar_listener(puerto, basedir, args.debug)


if __name__ == "__main__":
    main()
    