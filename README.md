# StitchBitch

Bild oder SVG -> Stickdatei. Lokales CLI, keine Cloud.

## Was es macht

- **Raster** (PNG/JPG/...): **1:1-Farbzuordnung** - jede Bildfarbe bekommt einen
  Faden derselben Farbe, auch Weiss. Die Palette wird nur aus flaechigen Pixeln
  gelernt (ein verbreiterter Kantenfilter schliesst Antialiasing konsequent aus,
  sonst entstehen Mischfarben als eigene Faeden = Flecken). Der vom Bildrand
  zusammenhaengende Hintergrund wird entfernt, **innenliegendes Weiss bleibt**
  (z. B. weisse Schrift); `--keep-background` stickt auch den Hintergrund.
  **Farb-Merge**: Farben
  innerhalb von +/-5 % (pro Kanal, einstellbar) werden zu einem Eintrag
  verschmolzen, dessen Wert der pixelgewichtete Durchschnitt (AVG) ist - das
  entfernt Farb-Artefakte aus Bild und Stitchdatei. Dann als Serpentinen-/
  Hatch-Fuellung gestickt. **Selbstpruefung**: die Farbanzahl wird automatisch
  erhoeht, bis die Fadenfarben das Bild treffen; die Trefferquote wird angezeigt.
  **Transparenz bleibt Transparenz**: nur deckende Pixel werden analysiert und
  gestickt - transparent ist keine Farbe und wird nie gestickt.
- **SVG**: gefuellte Pfade werden gefuellt, Striche als Laufstich obendrauf.
  Transforms, `viewBox`, Loecher (Even-Odd) werden unterstuetzt.
- **Ausgabe**: jedes [pyembroidery](https://github.com/EmbroidePy/pyembroidery)-Format,
  die Dateiendung entscheidet: `.dst` (Tajima, Standard), `.pes` (Brother),
  `.jef` (Janome), `.exp`, `.vp3`, `.pec`, `.xxx`, `.u01` ...

## Setup

```bash
cd ./StitchBitch
./run.sh --help                 # legt beim 1. Mal venv an + installiert, dann CLI
./run.sh --serve                # Web-UI auf http://127.0.0.1:8000
```

Manuell (ohne run.sh):

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Weitergeben

Es gibt nichts zu kompilieren (reines Python). Ordner weitergeben bzw. Tarball:

```bash
tar --exclude=.venv --exclude=__pycache__ --exclude=.git -czf StitchBitch.tar.gz StitchBitch   # im Elternordner ausfuehren
```

Der andere entpackt, hat Python 3 + Internet (nur beim ersten Start) und macht:

```bash
./run.sh --serve        # dann http://127.0.0.1:8000 im Browser
./run.sh bild.png -o bild.dst
```

## Benutzung

```bash
.venv/bin/python stitchbitch.py logo.png -o logo.dst
.venv/bin/python stitchbitch.py logo.svg -o logo.pes --width 80
.venv/bin/python stitchbitch.py foto.jpg -o foto.dst --colors 16 --width 120 \
    --stitch 2.5 --row 0.4 --angle 45 --preview foto.png
```

| Option | Default | Bedeutung |
|---|---|---|
| `-o/--output` | Eingabe mit `.dst` | Zieldatei, Endung = Format |
| `--width` | `100` | fertige Breite in mm |
| `--height` | `0` (egal) | max. Hoehe in mm |
| `--colors` | `16` | Farbanzahl fuer Rasterbilder (bis 64 sinnvoll) |
| `--merge-color` | `5` | Farben innerhalb +/-X % verschmelzen (Wert = Durchschnitt); `0` = aus |
| `--stitch` | auto | Stichlaenge in mm (Default: `Breite/120`, 0,5–3,0) |
| `--row` | auto | Reihenabstand der Fuellung in mm (Default: `Stich/4`, 0,25–0,5) |
| `--angle` | `45` | Fuellwinkel in Grad |
| `--rotate` | `0` | gesamtes Motiv um Grad drehen (Breite gilt vor der Drehung) |
| `--ignore-color RRGGBB` | – | diese Farbe komplett nicht sticken, mehrfach moeglich |
| `--keep-background` | – | randverbundenen Hintergrund NICHT entfernen (1:1, alles sticken) |
| `--outline` | – | zusaetzlich Randlinien je Farbe als Laufstich (fuer duenne Konturen) |
| `--border-color RRGGBB` | – | Patch-Rand entlang der Motiv-Silhouette in dieser Farbe |
| `--border-width` | `3` | Randstaerke in mm; `--width` zaehlt inkl. Rand |
| `--preview` | – | zusaetzlich Vorschau-PNG schreiben |

### Zusammenhaengende Stickerei (wenige Spruenge/Schnitte)

Die Stiche werden so geordnet, dass zusammenhaengende Bereiche als ein Faden
gestickt werden. Luecken, die von anderen Motivfarben bedeckt sind, werden kurz
ueberstochen (spaeter verdeckt), nur echte Hintergrund-Luecken werden gesprungen.
Statt ~1500 Schnitten pro Logo sind es jetzt typisch < 100.

### Gleiche Genauigkeit bei jeder Groesse

Die Stickdichte wird automatisch an die fertige Breite angepasst: Ziel sind
~120 Stiche ueber die Breite. Kleine Motive bekommen also kurze Stiche und enge
Reihen, grosse laengere — das Ergebnis ist unabhaengig von `--width` gleich
detailliert. Mit `--stitch`/`--row` kann man das fest ueberschreiben.
Untergrenze: ~0,5 mm Stichlaenge; darunter geht es physikalisch nicht feiner.

## Web-UI

```bash
.venv/bin/python stitchbitch.py --serve            # http://127.0.0.1:8000
.venv/bin/python stitchbitch.py --serve --port 9000
```

Browser auf, Datei waehlen, Parameter setzen, **Sticken**, dann Vorschau
ansehen und die Stickdatei herunterladen.

- **Motiv drehen**: Schieberegler unter dem Bild; die Vorschau dreht sofort mit,
  die fertige Datei wird beim Sticken entsprechend gedreht.
- **Palette**: nach der Dateiauswahl erscheinen die Farben als Kacheln (mit
  Pixelzahl; die wahrscheinliche Hintergrundfarbe ist mit `(BG)` markiert).
  Der Hintergrund ist standardmaessig **abgewaehlt** (nur die aussen liegende
  Flaeche verschwindet, innenliegendes Weiss bleibt). Wieder anhaken = 1:1.
  Weitere Farben abwaehlen entfernt sie komplett. Transparente Bereiche tauchen
  gar nicht erst auf (sie sind keine Farbe).

### Bild-Editor (im Browser, vor dem Sticken)

Reiter **Bearbeiten** links neben **Vorschau**. Alles clientseitig auf einem
Canvas, der Server bekommt danach das editierte PNG:

- **Zoom/Pan**: Mausrad = Zoom um den Cursor, `+`/`−`/`Fit`, Ziehen mit ✋.
- **🩹 Radierer (Skalpel)**: wegradieren (z. B. weiße Balken) -> wird transparent
  und daher nicht gestickt.
- **🖌️ Pinsel**: mit der Zielfarbe malen (Flächen ergänzen).
- **💧 Pipette**: Farbe unter dem Cursor aufnehmen (setzt auch die „ersetzen"-Quelle).
- **Farbe ersetzen**: aufgenommene Farbe global durch die Zielfarbe tauschen
  (z. B. Weiß -> Rot), inkl. Antialiasing-Toleranz.
- **↶ Undo**, **Reset** (Original), **Größe** = Pinselradius.
- **Patch-Rand**: Häkchen, Stärke in mm und Farbe. Legt einen Rand entlang der
  **Motiv-Silhouette** (nicht als Rechteck) an; die eingestellte Breite zählt als
  Gesamtgröße inkl. Rand.
- Nach dem Editieren ggf. **Palette aktualisieren**; dann **Stricken**.

Laeuft ohne Framework (nur Python-Stdlib, `http.server`) und ohne Upload zu
irgendeinem Server - alles lokal. Nur an `127.0.0.1` gebunden, keine
Authentifizierung.

## Grenzen (bewusst, v1)

- Nur eine Fuellart (Hatch). Kein Satin, keine Unterlage (Underlay), keine
  Pull-Compensation, keine Stickrichtung pro Form. -> fuer Logos/Flachfarben gut,
  fuer ordentliche Schrift/Feinheiten noch zu grob.
- SVG-Strichbreite wird ignoriert: dicker Strich = eine Laufstich-Linie.
- Trefferquote = Farbtreffer pro flaechigem Pixel, nicht Pixel-Diff der gerenderten
  Stiche. Treffer unter 100% heissen: die Palette konnte das Bild nicht exakt
  abbilden (z. B. Fotos/Gradienten).
- Hintergrunderkennung = haeufigste deckende Randfarbe (nur wenn >15% Flaeche);
  dient nur als `(BG)`-Hinweis und fuer `--remove-background`.
- Rasterkanten folgen dem Pixelraster; sehr grosse Bilder werden auf 2000 px
  heruntergerechnet (Analyse ist absichtlich gruendlicher, kostet ein paar Sekunden).
- Farbabstand ist RGB, nicht CIELAB (kein Farbwahrnehmungsmodell).
- Farb-Merge-Schwelle ist pro Kanal (max. Kanal-Differenz), nicht euklidisch.
- Fadenfarben = Bildfarben, kein Abgleich mit echter Garn-Palette.

Sinnvolle naechste Schritte, falls gebraucht: Satin/Underlay fuer bessere
Qualitaet, Garn-Palettenabgleich, mehrere Motive pro Datei.
