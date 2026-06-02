Modos de ejecución:

python3 lee_satres.py              # modo daemon: log a fichero, silencioso
python3 lee_satres.py --debug      # modo depuración: log detallado en consola

Log en modo daemon:
 <basedir>/log/lee_satres.log con rotación diaria a medianoche UTC, 30 días de retención. En operación normal solo registra el arranque, el cierre de ficheros y un mensaje por ciclo %0>.

Instalación del servicio de usuario:

# Copiar el script y el ini (ajustar ruta si es necesario)
mkdir -p ~/twstft
cp lee_satres.py twstft.ini ~/twstft/
chmod +x ~/twstft/lee_satres.py

# Instalar el servicio
mkdir -p ~/.config/systemd/user/
cp lee_satres.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable lee_satres
systemctl --user start lee_satres

# Consultar estado y logs
systemctl --user status lee_satres
journalctl --user -u lee_satres -f
