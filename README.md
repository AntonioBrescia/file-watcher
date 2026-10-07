# File Watcher

Utility **standalone per Windows** che monitora una cartella (tipicamente `Downloads`) e, quando compare un file, lo **sposta** in un'altra cartella oppure lo **invia a una stampante di rete**.

Vive nell'area di notifica (vicino all'orologio) e ha una piccola finestra con la cronologia dei file processati.

Nasce come evoluzione di due script `.bat` (MS-DOS) in un'applicazione con interfaccia grafica, regole configurabili e possibilità di ripetere le operazioni fallite.

## Funzionalità

- Controllo della cartella ogni pochi secondi (intervallo configurabile)
- **Due azioni**: `sposta` (con rinomina opzionale) e `stampa` (porta LPT + stampante di rete condivisa)
- **File sentinella**: il browser scarica prima il file e poi la sentinella; il file viene processato solo quando esistono entrambi, così non si legge mai un download incompleto
- **Regole multiple e configurabili** dalla finestra Impostazioni (nessun parametro hardcoded)
- Icona nella **system tray** con menu: Apri, Impostazioni, Pausa, Esci
- Finestra con **cronologia** (data e ora, regola, file, esito, dettaglio); gli errori sono in rosso e segnalati anche con una notifica
- **Ripeti selezionato**: rilancia l'azione su un file già processato, utile dopo un errore (stampante spenta, rete assente...)
- **Istanza unica**: non si possono avviare due copie contemporaneamente
- Distribuibile come **singolo `.exe`**, senza installazione

## Come funziona

```
Ogni N secondi, per ogni regola attiva:
  se esistono il file E la sentinella:
      1. sposta il file nella cartella locale "archivio" (con data e ora nel nome)
      2. elimina la sentinella
      3. esegue l'azione sul file archiviato (sposta / stampa)
      4. registra l'esito nella cronologia
```

**Perché l'archivio locale?** Il file viene tolto da `Downloads` ma ne resta una copia in `archivio/`. Grazie a questa copia il tasto **Ripeti** funziona anche se l'operazione originale era già andata a buon fine o era fallita. Vengono conservati solo gli ultimi N file (default 50).

**Architettura**

| Parte | Descrizione |
|---|---|
| Thread di monitoraggio | Controlla le regole in background, la GUI non si blocca |
| Thread principale | Interfaccia `tkinter` |
| Thread tray | Icona `pystray` |
| Coda (`queue.Queue`) | Tray e monitor inviano i comandi alla GUI, perché `tkinter` non è thread-safe |

## Requisiti

- Windows 10/11
- Python 3.9+ (solo per eseguire dai sorgenti o per creare l'exe)

## Avvio dai sorgenti

```bat
pip install -r requirements.txt
python file_watcher.py
```

All'avvio non compare nessuna finestra: cerca l'icona blu vicino all'orologio (eventualmente dentro la freccia "mostra icone nascoste") e fai doppio clic.

## Creare l'eseguibile standalone

```bat
pip install -r requirements.txt
build.bat
```

Il file `dist\FileWatcher.exe` è l'unico file da distribuire: non richiede installazione né Python sul PC di destinazione. Al primo avvio crea accanto a sé `config.json`, `cronologia.json` e la cartella `archivio\`.

## Configurazione

Le impostazioni si modificano dalla finestra **Impostazioni** (salvate in `config.json`).

**Generali**

| Parametro | Descrizione | Default |
|---|---|---|
| Controlla ogni (secondi) | Frequenza del controllo | 3 |
| File da tenere in archivio | Quante copie conservare per "Ripeti" | 50 |

**Per ogni regola**

| Campo | Descrizione | Esempio |
|---|---|---|
| Nome regola | Nome univoco | `Scontrini cassa (stampa)` |
| Regola attiva | Abilita/disabilita la regola | |
| Cartella da controllare | Supporta variabili d'ambiente | `%UserProfile%\Downloads` |
| Nome del file | File da cercare | `scontr00.001` |
| File sentinella | Opzionale; se vuoto si verifica che il file non sia in uso | `scontr00.on` |
| Azione | `sposta` oppure `stampa` | `stampa` |
| Cartella di destinazione | Solo per `sposta` | `C:\SwInstallato\Olivetti\ElaExecute\EE_IN` |
| Rinomina in | Solo per `sposta`, opzionale | `scontrino.Xml` |
| Porta stampante | Solo per `stampa` | `LPT2` |
| Stampante di rete | Solo per `stampa` | `\\Eurotec-Master\CassaCustom` |

I due esempi dei file `.bat` originali sono già inseriti come regole di default al primo avvio.

### Equivalenza con i file .bat

| Batch | Python |
|---|---|
| `IF EXIST sentinella / IF EXIST file` | `cerca_file_pronto()` |
| `NET USE LPT2: \\pc\stampante` | `azione_stampa()` (una volta sola, non a ogni ciclo) |
| `COPY file LPT2` | `copy /b` lanciato con `subprocess` |
| `XCOPY` + `REN` | `shutil.copy2` verso il nome nuovo |
| `DEL file` / `DEL sentinella` | spostamento in archivio + `os.remove` |
| `PING ... GOTO esegui` | `stop.wait(N)` nel thread di monitoraggio |

## Test rapido

1. Avvia l'app e apri **Impostazioni**.
2. Imposta una regola `sposta` con una cartella di destinazione di prova.
3. Crea in `Downloads` prima il file (es. `scontr00mag.Xml`) e poi la sentinella (`scontr00mag.ok`).
4. Entro pochi secondi il file compare nella destinazione con il nuovo nome e nella cronologia appare la riga **OK**.
5. Selezionala e premi **Ripeti selezionato** per rifare l'operazione.

## Struttura del repository

```
file_watcher.py    codice dell'applicazione (tutto commentato)
requirements.txt   dipendenze
build.bat          crea l'exe con PyInstaller
README.md
.gitignore
```

## Possibili miglioramenti

- Avvio automatico con Windows
- Log su file con rotazione
- Uso di `watchdog` al posto del polling
- Più tentativi automatici prima di segnalare l'errore
- Stampa con le API di Windows invece di `copy /b` per stampanti non testuali

## Autore

Antonio Brescia – [GitHub](https://github.com/AntonioBrescia)
