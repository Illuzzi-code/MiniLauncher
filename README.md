# Mini Launcher de Minecraft

Un launcher de Minecraft hecho en Python, con interfaz en Tkinter. Lo escribí yo, Ericson, para tener un launcher propio donde cada perfil tiene sus mods, sus mundos y su configuración separados, sin depender de nada más pesado.

Todo está en un solo archivo (`launcher6.py`).

## Qué hace

- Descarga e instala cualquier versión de Minecraft (release, snapshot, beta y alpha).
- Soporta Vanilla, Fabric y Forge.
- Perfiles independientes: cada uno tiene su propia carpeta de juego (mods, mundos, capturas, `options.txt`). Se pueden crear, borrar y duplicar, con la opción de copiar o no los mundos.
- Buscador de mods con Modrinth integrado: buscar, ver detalle, instalar, desactivar, borrar y revisar actualizaciones, sin salir del launcher.
- Instala solos unos cuantos mods de rendimiento (Sodium, Lithium, FerriteCore, ModernFix y similares) en los perfiles que lo permitan.
- Calcula la RAM según el equipo, o se puede poner a mano por perfil.
- Cuenta offline o cuenta Microsoft.
- Si el juego se cierra con error, lee el log y el último crash report e intenta explicar qué pasó.
- Se puede jugar sin internet con las versiones que ya estén descargadas.

## Cómo está armado

El archivo se puede leer de arriba abajo en este orden:

1. **Configuración y utilidades**: carga y guarda `launcher_config.json`, genera el UUID offline y abre carpetas según el sistema operativo.
2. **Rendimiento**: detecta la RAM del equipo (con `ctypes` en Windows, `sysctl` en Mac y `/proc/meminfo` en Linux) y arma los argumentos de la JVM. Con 4 GB o más usa los flags de G1 de Aikar; con menos, solo los básicos.
3. **Diagnóstico de crashes**: una lista de expresiones regulares que reconocen los errores más comunes (memoria, versión de Java, mods incompatibles, Mixin, drivers de video) y devuelven una explicación en español.
4. **Cuenta Microsoft**: flujo de código de dispositivo, sin librerías extra.
5. **Modrinth**: cliente de la API, descarga con verificación SHA-1 y elección del archivo correcto según versión y loader.
6. **Interfaz**: tema oscuro propio, botones redondeados dibujados sobre `Canvas`, listas con scroll y carga de iconos en segundo plano.
7. **Clases principales**: `ModsTab` (la pestaña de mods) y `Launcher` (la ventana principal, que une todo lo demás).

Para descargar versiones y construir el comando de arranque uso [`minecraft-launcher-lib`](https://pypi.org/project/minecraft-launcher-lib/). Todo lo demás lo fui armando alrededor de eso.

## Cómo se fue desarrollando

Empecé con lo mínimo: una ventana con un usuario, una lista de versiones y un botón de jugar. Con eso ya se podía instalar y abrir el juego, pero era muy básico.

De ahí fui añadiendo cosas a medida que las necesitaba, y se nota en el código. Por ejemplo, al principio la RAM era un ajuste global y luego la pasé a cada perfil. Por eso `load_config()` tiene una parte que migra las configuraciones viejas: no quería que alguien que ya tuviera su archivo de configuración perdiera sus datos.

Los perfiles llegaron antes que los mods, porque tenía poco sentido instalar mods si todo se mezclaba en una sola carpeta. Una vez separadas las instancias, la pestaña de mods se volvió bastante natural.

Algunas decisiones que tomé en el camino:

- **Todo se descarga una sola vez.** Versiones, librerías, assets y Java viven en una carpeta común (`mi_launcher_mc`), y lo único que cambia entre perfiles es la carpeta de juego.
- **Nunca pisar lo del jugador.** El `options.txt` inicial solo se escribe si el perfil aún no tiene uno.
- **Si Java no arranca, reintentar.** Cuando la JVM rechaza los parámetros, el launcher vuelve a intentarlo una vez con menos RAM y sin los flags de optimización, antes de dar error.
- **Seguridad de red por defecto.** Se verifican siempre los certificados HTTPS. Hay una constante `ALLOW_INSECURE_SSL` por si un antivirus o proxy los rompe, pero viene en `False` y es mejor dejarla así.
- **La interfaz nunca se bloquea.** Instalar, copiar perfiles, buscar mods y leer el juego corren en hilos aparte y vuelven al hilo principal con `after()`.
- **El diagnóstico de crashes** lo agregué después de cansarme de abrir los crash reports a mano para ver siempre lo mismo.

## Requisitos

- Python 3.9 o superior (con Tkinter, que viene incluido en la instalación normal de Python en Windows y Mac).
- `minecraft-launcher-lib`
- `pillow` (opcional, solo para ver los iconos de los mods)
- `certifi` (opcional, ayuda con los certificados si el sistema no trae los suyos)

```bash
pip install minecraft-launcher-lib pillow certifi
```

## Uso

```bash
python launcher6.py
```

La primera vez que lances una versión va a descargar todo lo necesario, incluido el Java que le toque, así que tarda más. Las siguientes veces es rápido.

Los datos se guardan en una carpeta `mi_launcher_mc` dentro del directorio de datos de Minecraft de tu sistema:

```
mi_launcher_mc/
├── versions/ libraries/ assets/ runtime/   <- compartido por todos los perfiles
├── instances/<perfil>/                     <- mods, saves, options.txt, etc.
├── launcher_config.json                    <- usuario, perfiles y ajustes
└── ms_auth.json                            <- sesión de Microsoft (si la usas)
```

## Cuenta Microsoft

El inicio de sesión con Microsoft necesita un Client ID de una app registrada en Azure, y Mojang además exige aprobar esa app para usar su API (más info en `aka.ms/AppRegInfo`). Sin esa aprobación el login falla con el error `Invalid app registration`. Si no quieres lidiar con eso, el modo offline funciona sin nada de esto.

## Notas

- Es un proyecto personal. No tiene relación con Mojang ni con Microsoft.
- El modo offline es para jugar en local o en servidores que lo permitan. Si quieres jugar en servidores oficiales, necesitas una cuenta de Minecraft legítima.
- Los mods se descargan de Modrinth; cada mod tiene su propia licencia y su autor.

## Autor

Ericson
