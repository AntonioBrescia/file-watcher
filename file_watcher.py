# -*- coding: utf-8 -*-
"""
FILE WATCHER - utility per Windows che monitora una cartella.

Ogni pochi secondi controlla se è presente un file (e il relativo file
"sentinella" che indica che il download è completato). Se sì, lo archivia
in locale e poi esegue un'azione:
    - "sposta": copia il file in un'altra cartella (con eventuale rinomina)
    - "stampa": invia il file a una stampante di rete

L'app vive nella tray (vicino all'orologio) e ha una piccola finestra con:
    - la cronologia degli ultimi file processati
    - il tasto Impostazioni
    - il tasto "Ripeti selezionato" per rilanciare un file già processato

Librerie esterne: pystray e Pillow (vedi requirements.txt).
"""

import copy
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from tkinter import ttk, messagebox

import pystray
from PIL import Image, ImageDraw


# ======================================================================
# 1. PERCORSI E CONFIGURAZIONE
# ======================================================================

def cartella_base():
    """Cartella dove salvare config, cronologia e archivio.

    Se l'app è stata trasformata in .exe con PyInstaller ("frozen") usiamo
    la cartella dell'exe, altrimenti la cartella dello script .py.
    Così i file di configurazione stanno sempre accanto al programma.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = cartella_base()
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
CRONOLOGIA_PATH = os.path.join(BASE_DIR, "cronologia.json")
ARCHIVIO_DIR = os.path.join(BASE_DIR, "archivio")

MAX_VOCI_CRONOLOGIA = 200  # quante righe di cronologia conservare

# Configurazione di partenza: contiene i due esempi dei file .bat.
# Viene scritta in config.json al primo avvio.
CONFIG_DEFAULT = {
    "intervallo_secondi": 3,      # ogni quanti secondi controllare le cartelle
    "archivio_max_file": 50,      # quanti file tenere nella cartella archivio
    "regole": [
        {
            # Esempio 1: invio a stampante di rete (Generic Text)
            "nome": "Scontrini cassa (stampa)",
            "attiva": True,
            "origine": "%UserProfile%\\Downloads",
            "file": "scontr00.001",
            "sentinella": "scontr00.on",
            "azione": "stampa",
            "destinazione": "",
            "rinomina_in": "",
            "porta": "LPT2",
            "stampante_rete": "\\\\Eurotec-Master\\CassaCustom",
        },
        {
            # Esempio 2: sposta il file in un'altra cartella cambiandone il nome
            "nome": "Scontrino XML (sposta)",
            "attiva": True,
            "origine": "%UserProfile%\\Downloads",
            "file": "scontr00mag.Xml",
            "sentinella": "scontr00mag.ok",
            "azione": "sposta",
            "destinazione": "C:\\SwInstallato\\Olivetti\\ElaExecute\\EE_IN",
            "rinomina_in": "scontrino.Xml",
            "porta": "",
            "stampante_rete": "",
        },
    ],
}

# Valori di una regola nuova creata dalla finestra impostazioni
REGOLA_VUOTA = {
    "nome": "Nuova regola",
    "attiva": True,
    "origine": "%UserProfile%\\Downloads",
    "file": "",
    "sentinella": "",
    "azione": "sposta",
    "destinazione": "",
    "rinomina_in": "",
    "porta": "",
    "stampante_rete": "",
}


def leggi_json(percorso, default):
    """Legge un file JSON; se manca o è rovinato restituisce 'default'."""
    try:
        with open(percorso, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def scrivi_json(percorso, dati):
    """Scrive un file JSON (in caso di errore di scrittura non blocca l'app)."""
    try:
        with open(percorso, "w", encoding="utf-8") as f:
            json.dump(dati, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


def carica_config():
    """Carica config.json; al primo avvio lo crea con i valori di default."""
    config = leggi_json(CONFIG_PATH, None)
    if not isinstance(config, dict) or "regole" not in config:
        config = copy.deepcopy(CONFIG_DEFAULT)
        scrivi_json(CONFIG_PATH, config)
    return config


def espandi(percorso):
    """Sostituisce variabili tipo %UserProfile% con il valore reale."""
    return os.path.expandvars(os.path.expanduser(percorso))


# ======================================================================
# 2. CRONOLOGIA
# ======================================================================

# Lista delle voci, la più recente per prima. Ogni voce è un dizionario.
CRONOLOGIA = leggi_json(CRONOLOGIA_PATH, [])
LOCK_CRONOLOGIA = threading.Lock()  # la usano sia il monitor sia la GUI


def nuova_voce(regola, file, ok, messaggio, archivio):
    """Crea una voce di cronologia."""
    return {
        "data": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "regola": regola,
        "file": file,
        "esito_ok": ok,
        "messaggio": messaggio,
        "archivio": archivio,  # copia locale usata da "Ripeti"
    }


def registra(voce, coda_ui):
    """Aggiunge la voce in cima alla cronologia, la salva e avvisa la GUI."""
    with LOCK_CRONOLOGIA:
        CRONOLOGIA.insert(0, voce)
        del CRONOLOGIA[MAX_VOCI_CRONOLOGIA:]  # tiene solo le ultime N
        scrivi_json(CRONOLOGIA_PATH, CRONOLOGIA)
    coda_ui.put(("aggiorna", voce))


# ======================================================================
# 3. LOGICA DI ELABORAZIONE FILE
# ======================================================================

# Evita che il monitor e il tasto "Ripeti" usino la stampante insieme
LOCK_AZIONI = threading.Lock()

# Ricorda quali porte stampante sono già state collegate con "net use",
# così non rifacciamo la mappatura a ogni file.
PORTE_MAPPATE = set()


def file_bloccato(percorso):
    """True se il file è ancora in uso (es. il browser lo sta scrivendo)."""
    try:
        with open(percorso, "ab"):
            return False
    except OSError:
        return True


def cerca_file_pronto(regola):
    """Controlla se il file della regola è pronto da processare.

    Restituisce (percorso_file, percorso_sentinella) oppure None.
    Se è indicata una sentinella, il file è pronto solo se esiste anche
    quella (il browser la scarica DOPO il file principale).
    Senza sentinella proviamo ad aprire il file per vedere se è libero.
    """
    nome = regola["file"].strip()
    if not nome:
        return None

    cartella = espandi(regola["origine"])
    percorso = os.path.join(cartella, nome)
    if not os.path.isfile(percorso):
        return None

    sentinella = regola.get("sentinella", "").strip()
    if sentinella:
        p_sentinella = os.path.join(cartella, sentinella)
        if not os.path.isfile(p_sentinella):
            return None  # il download non è ancora completo
        return percorso, p_sentinella

    if file_bloccato(percorso):
        return None
    return percorso, None


def lancia(argomenti):
    """Esegue un comando di Windows senza mostrare la finestra nera.

    Restituisce (codice_uscita, testo_output).
    """
    try:
        r = subprocess.run(
            argomenti,
            capture_output=True,
            text=True,
            encoding="cp850",          # codifica dei comandi DOS in italiano
            errors="replace",
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def azione_sposta(regola, file_archiviato):
    """Copia il file nella cartella di destinazione (rinominandolo se serve)."""
    destinazione = espandi(regola["destinazione"]).strip()
    if not destinazione:
        return False, "Cartella di destinazione non impostata"

    os.makedirs(destinazione, exist_ok=True)
    nome_finale = regola.get("rinomina_in", "").strip() or regola["file"]
    percorso_finale = os.path.join(destinazione, nome_finale)
    shutil.copy2(file_archiviato, percorso_finale)
    return True, "Copiato in " + percorso_finale


def azione_stampa(regola, file_archiviato):
    """Invia il file alla stampante di rete (come fanno i .bat con COPY LPT2)."""
    porta = regola.get("porta", "").strip().rstrip(":").upper()
    stampante = regola.get("stampante_rete", "").strip()
    chiave = porta + "|" + stampante

    if porta:
        if not stampante:
            return False, "Stampante di rete non impostata"
        # Collega la porta (es. LPT2) alla stampante condivisa, una sola volta
        if chiave not in PORTE_MAPPATE:
            lancia(["net", "use", porta, "/delete", "/y"])
            codice, out = lancia(
                ["net", "use", porta + ":", stampante, "/persistent:yes"]
            )
            if codice != 0:
                return False, "Collegamento stampante fallito: " + out
            PORTE_MAPPATE.add(chiave)
        destinazione = porta
    elif stampante:
        destinazione = stampante  # copia diretta sul percorso di rete
    else:
        return False, "Nessuna stampante impostata"

    # "copy /b" invia il file così com'è (binario) alla porta/stampante
    codice, out = lancia(["cmd", "/c", "copy", "/b", file_archiviato, destinazione])
    if codice != 0:
        PORTE_MAPPATE.discard(chiave)  # al prossimo giro rifà il collegamento
        return False, "Stampa fallita: " + (out or "errore sconosciuto")
    return True, "Inviato a " + destinazione


def esegui_azione(regola, file_archiviato):
    """Esegue l'azione della regola. Restituisce (ok, messaggio)."""
    with LOCK_AZIONI:
        try:
            if regola["azione"] == "sposta":
                return azione_sposta(regola, file_archiviato)
            if regola["azione"] == "stampa":
                return azione_stampa(regola, file_archiviato)
            return False, "Azione sconosciuta: " + str(regola["azione"])
        except Exception as e:  # qualsiasi errore diventa un esito "fallito"
            return False, str(e)


def pulisci_archivio(max_file):
    """Cancella i file più vecchi dell'archivio, tenendo solo gli ultimi N."""
    try:
        percorsi = [os.path.join(ARCHIVIO_DIR, n) for n in os.listdir(ARCHIVIO_DIR)]
        percorsi.sort(key=os.path.getmtime, reverse=True)  # i più nuovi per primi
        for vecchio in percorsi[max_file:]:
            os.remove(vecchio)
    except OSError:
        pass


def processa_regola(regola, max_archivio):
    """Se il file della regola è pronto lo archivia ed esegue l'azione.

    Restituisce la voce di cronologia, oppure None se non c'era nulla da fare.
    """
    pronto = cerca_file_pronto(regola)
    if pronto is None:
        return None
    percorso, sentinella = pronto

    # 1) Sposta il file nell'archivio locale con data e ora nel nome.
    #    Così lo togliamo da Downloads ma ne teniamo una copia per "Ripeti".
    os.makedirs(ARCHIVIO_DIR, exist_ok=True)
    marca = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    archiviato = os.path.join(ARCHIVIO_DIR, marca + "_" + regola["file"])
    try:
        shutil.move(percorso, archiviato)
    except OSError:
        return None  # file ancora in uso: riproviamo al prossimo controllo

    # 2) Elimina la sentinella, altrimenti verrebbe rielaborato all'infinito
    if sentinella:
        try:
            os.remove(sentinella)
        except OSError:
            pass

    # 3) Esegue davvero l'azione (sposta o stampa) sul file archiviato
    ok, messaggio = esegui_azione(regola, archiviato)
    pulisci_archivio(max_archivio)
    return nuova_voce(regola["nome"], regola["file"], ok, messaggio, archiviato)


def ciclo_monitoraggio(stato, coda_ui, stop):
    """Thread che controlla le regole ogni N secondi finché l'app è aperta."""
    while not stop.is_set():
        if not stato["in_pausa"]:
            config = stato["config"]
            for regola in config["regole"]:
                if not regola.get("attiva", True):
                    continue
                try:
                    voce = processa_regola(regola, config["archivio_max_file"])
                except Exception:
                    voce = None  # un errore imprevisto non deve fermare il thread
                if voce:
                    registra(voce, coda_ui)
        # Attende N secondi, ma si sveglia subito se l'app sta chiudendo
        stop.wait(max(1, int(stato["config"]["intervallo_secondi"])))


# ======================================================================
# 4. FINESTRA IMPOSTAZIONI
# ======================================================================

# (chiave nel config, etichetta mostrata, suggerimento)
CAMPI = [
    ("nome", "Nome regola", ""),
    ("origine", "Cartella da controllare", "es. %UserProfile%\\Downloads"),
    ("file", "Nome del file", "es. scontr00.001"),
    ("sentinella", "File sentinella", "opzionale: il file è pronto solo se esiste anche questo"),
    ("azione", "Azione", "sposta oppure stampa"),
    ("destinazione", "Cartella di destinazione", "solo per 'sposta'"),
    ("rinomina_in", "Rinomina in", "solo per 'sposta', opzionale"),
    ("porta", "Porta stampante", "solo per 'stampa', es. LPT2"),
    ("stampante_rete", "Stampante di rete", "solo per 'stampa', es. \\\\PC\\Stampante"),
]


class FinestraImpostazioni(tk.Toplevel):
    """Finestra per modificare regole e parametri generali."""

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("Impostazioni")
        self.geometry("780x520")
        self.transient(app.root)   # resta sopra la finestra principale
        self.grab_set()            # blocca la finestra principale finché è aperta

        # Lavoriamo su una COPIA: se l'utente annulla, nulla viene modificato
        self.copia = copy.deepcopy(app.stato["config"])
        self.indice = None         # regola attualmente mostrata nei campi

        self.vars = {c[0]: tk.StringVar() for c in CAMPI}
        self.var_attiva = tk.BooleanVar()
        self.vars["nome"].trace_add("write", self.aggiorna_nome_in_lista)

        self.costruisci()
        self.riempi_lista(0 if self.copia["regole"] else None)

    # ---------- costruzione grafica ----------
    def costruisci(self):
        # Colonna sinistra: elenco regole + pulsanti
        sinistra = ttk.Frame(self, padding=8)
        sinistra.pack(side="left", fill="y")
        ttk.Label(sinistra, text="Regole").pack(anchor="w")
        self.lista = tk.Listbox(sinistra, width=28, exportselection=False)
        self.lista.pack(fill="y", expand=True, pady=4)
        self.lista.bind("<<ListboxSelect>>", self.al_cambio_selezione)
        ttk.Button(sinistra, text="Nuova regola", command=self.nuova_regola).pack(fill="x")
        ttk.Button(sinistra, text="Elimina regola", command=self.elimina_regola).pack(fill="x", pady=2)

        # Colonna destra: campi della regola + parametri generali
        destra = ttk.Frame(self, padding=8)
        destra.pack(side="left", fill="both", expand=True)

        ttk.Checkbutton(destra, text="Regola attiva", variable=self.var_attiva).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=2)

        riga = 1
        for chiave, etichetta, suggerimento in CAMPI:
            ttk.Label(destra, text=etichetta).grid(row=riga, column=0, sticky="w", pady=2)
            if chiave == "azione":
                campo = ttk.Combobox(destra, textvariable=self.vars[chiave],
                                     values=["sposta", "stampa"], state="readonly", width=47)
            else:
                campo = ttk.Entry(destra, textvariable=self.vars[chiave], width=50)
            campo.grid(row=riga, column=1, sticky="we", padx=6)
            if suggerimento:
                ttk.Label(destra, text=suggerimento, foreground="gray").grid(
                    row=riga + 1, column=1, sticky="w", padx=6)
            riga += 2
        destra.columnconfigure(1, weight=1)

        # Parametri generali
        generali = ttk.LabelFrame(destra, text="Generali", padding=6)
        generali.grid(row=riga, column=0, columnspan=2, sticky="we", pady=8)
        self.var_intervallo = tk.StringVar(value=str(self.copia["intervallo_secondi"]))
        self.var_archivio = tk.StringVar(value=str(self.copia["archivio_max_file"]))
        ttk.Label(generali, text="Controlla ogni (secondi)").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(generali, from_=1, to=3600, width=6,
                    textvariable=self.var_intervallo).grid(row=0, column=1, padx=6)
        ttk.Label(generali, text="File da tenere in archivio").grid(row=1, column=0, sticky="w")
        ttk.Spinbox(generali, from_=1, to=10000, width=6,
                    textvariable=self.var_archivio).grid(row=1, column=1, padx=6)

        # Pulsanti finali
        pulsanti = ttk.Frame(destra)
        pulsanti.grid(row=riga + 1, column=0, columnspan=2, sticky="e")
        ttk.Button(pulsanti, text="Salva", command=self.salva_e_chiudi).pack(side="left", padx=4)
        ttk.Button(pulsanti, text="Annulla", command=self.destroy).pack(side="left")

    # ---------- gestione elenco regole ----------
    def riempi_lista(self, seleziona):
        """Ricostruisce l'elenco e seleziona la regola indicata."""
        self.lista.delete(0, tk.END)
        for r in self.copia["regole"]:
            self.lista.insert(tk.END, r["nome"])
        self.indice = None
        if seleziona is not None:
            self.lista.selection_set(seleziona)
            self.indice = seleziona
            self.carica_campi()

    def al_cambio_selezione(self, _evento):
        """Cliccando un'altra regola: salva i campi della vecchia, mostra la nuova."""
        selezione = self.lista.curselection()
        if not selezione:
            return
        self.salva_campi()
        self.indice = selezione[0]
        self.carica_campi()

    def aggiorna_nome_in_lista(self, *_):
        """Mentre scrivi il nome, aggiorna anche il testo nell'elenco a sinistra."""
        if self.indice is None:
            return
        self.lista.delete(self.indice)
        self.lista.insert(self.indice, self.vars["nome"].get())
        self.lista.selection_set(self.indice)

    def carica_campi(self):
        """Copia i valori della regola selezionata nei campi."""
        regola = self.copia["regole"][self.indice]
        for chiave, _, _ in CAMPI:
            self.vars[chiave].set(regola.get(chiave, ""))
        self.var_attiva.set(regola.get("attiva", True))

    def salva_campi(self):
        """Copia i valori dei campi dentro la regola selezionata."""
        if self.indice is None or self.indice >= len(self.copia["regole"]):
            return
        regola = self.copia["regole"][self.indice]
        for chiave, _, _ in CAMPI:
            regola[chiave] = self.vars[chiave].get().strip()
        regola["attiva"] = self.var_attiva.get()

    def nuova_regola(self):
        self.salva_campi()
        self.copia["regole"].append(copy.deepcopy(REGOLA_VUOTA))
        self.riempi_lista(len(self.copia["regole"]) - 1)

    def elimina_regola(self):
        if self.indice is None:
            return
        if not messagebox.askyesno("Elimina", "Eliminare la regola selezionata?", parent=self):
            return
        del self.copia["regole"][self.indice]
        self.indice = None
        self.riempi_lista(0 if self.copia["regole"] else None)

    # ---------- salvataggio ----------
    def salva_e_chiudi(self):
        self.salva_campi()

        # Controlli: nomi non vuoti e senza doppioni (servono per "Ripeti")
        nomi = [r["nome"] for r in self.copia["regole"]]
        if "" in nomi or len(nomi) != len(set(nomi)):
            messagebox.showerror("Errore", "Ogni regola deve avere un nome diverso.", parent=self)
            return
        try:
            self.copia["intervallo_secondi"] = max(1, int(self.var_intervallo.get()))
            self.copia["archivio_max_file"] = max(1, int(self.var_archivio.get()))
        except ValueError:
            messagebox.showerror("Errore", "Intervallo e archivio devono essere numeri.", parent=self)
            return

        self.app.applica_config(self.copia)
        self.destroy()


# ======================================================================
# 5. APPLICAZIONE PRINCIPALE (finestra + icona nella tray)
# ======================================================================

def crea_icona():
    """Disegna una piccola icona (cerchio blu con un foglio bianco)."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=(30, 100, 200, 255))
    d.rectangle((20, 14, 44, 50), fill="white")
    for y in (22, 30, 38):
        d.line((25, y, 39, y), fill=(30, 100, 200, 255), width=2)
    return img


class App:
    def __init__(self):
        # Stato condiviso con il thread di monitoraggio
        self.stato = {"config": carica_config(), "in_pausa": False}
        # Coda: da qui il thread e la tray mandano comandi alla finestra.
        # (tkinter non è "thread-safe", quindi solo il thread principale lo tocca)
        self.coda_ui = queue.Queue()
        self.stop = threading.Event()
        self.voci_per_riga = {}  # collega la riga della tabella alla sua voce

        # Finestra principale: parte NASCOSTA, si vede solo l'icona in tray
        self.root = tk.Tk()
        self.root.title("File Watcher")
        self.root.geometry("820x380")
        self.root.protocol("WM_DELETE_WINDOW", self.nascondi)  # la X nasconde, non chiude
        self.costruisci_finestra()
        self.root.withdraw()

        # Icona nella tray con menu col tasto destro
        menu = pystray.Menu(
            pystray.MenuItem("Apri", lambda i, m: self.coda_ui.put(("mostra",)), default=True),
            pystray.MenuItem("Impostazioni", lambda i, m: self.coda_ui.put(("impostazioni",))),
            pystray.MenuItem("Pausa", lambda i, m: self.coda_ui.put(("pausa",)),
                             checked=lambda item: self.stato["in_pausa"]),
            pystray.MenuItem("Esci", lambda i, m: self.coda_ui.put(("esci",))),
        )
        self.icona = pystray.Icon("filewatcher", crea_icona(), "File Watcher", menu)
        self.icona.run_detached()  # la tray gira in un suo thread

        # Avvia il thread che controlla le cartelle
        threading.Thread(target=ciclo_monitoraggio,
                         args=(self.stato, self.coda_ui, self.stop),
                         daemon=True).start()

        self.ricarica_lista()
        self.root.after(300, self.leggi_coda)  # controlla la coda ogni 300 ms

    # ---------- finestra principale ----------
    def costruisci_finestra(self):
        # Tabella con la cronologia
        centro = ttk.Frame(self.root, padding=6)
        centro.pack(fill="both", expand=True)
        colonne = ("data", "regola", "file", "esito", "dettaglio")
        self.tabella = ttk.Treeview(centro, columns=colonne, show="headings", selectmode="browse")
        intestazioni = {"data": ("Data e ora", 140), "regola": ("Regola", 150),
                        "file": ("File", 110), "esito": ("Esito", 60),
                        "dettaglio": ("Dettaglio", 330)}
        for col, (testo, larghezza) in intestazioni.items():
            self.tabella.heading(col, text=testo)
            self.tabella.column(col, width=larghezza, anchor="w")
        self.tabella.tag_configure("errore", foreground="red")
        barra = ttk.Scrollbar(centro, orient="vertical", command=self.tabella.yview)
        self.tabella.configure(yscrollcommand=barra.set)
        self.tabella.pack(side="left", fill="both", expand=True)
        barra.pack(side="right", fill="y")

        # Pulsanti in basso
        basso = ttk.Frame(self.root, padding=6)
        basso.pack(fill="x")
        ttk.Button(basso, text="Impostazioni", command=self.apri_impostazioni).pack(side="left")
        ttk.Button(basso, text="Ripeti selezionato", command=self.ripeti).pack(side="left", padx=6)
        self.var_pausa = tk.BooleanVar(value=False)
        ttk.Checkbutton(basso, text="Metti in pausa il monitoraggio",
                        variable=self.var_pausa, command=self.cambia_pausa).pack(side="right")

    def ricarica_lista(self):
        """Ridisegna la tabella leggendo la cronologia (più recenti in alto)."""
        self.tabella.delete(*self.tabella.get_children())
        self.voci_per_riga.clear()
        with LOCK_CRONOLOGIA:
            voci = list(CRONOLOGIA)
        for voce in voci:
            tag = () if voce["esito_ok"] else ("errore",)
            riga = self.tabella.insert("", "end", tags=tag, values=(
                voce["data"], voce["regola"], voce["file"],
                "OK" if voce["esito_ok"] else "ERRORE", voce["messaggio"]))
            self.voci_per_riga[riga] = voce

    # ---------- comandi arrivati da tray / thread ----------
    def leggi_coda(self):
        """Gira nel thread principale: esegue i comandi messi in coda."""
        while True:
            try:
                comando = self.coda_ui.get_nowait()
            except queue.Empty:
                break
            tipo = comando[0]
            if tipo == "mostra":
                self.mostra()
            elif tipo == "impostazioni":
                self.mostra()
                self.apri_impostazioni()
            elif tipo == "pausa":
                self.var_pausa.set(not self.stato["in_pausa"])
                self.cambia_pausa()
            elif tipo == "aggiorna":
                self.ricarica_lista()
                voce = comando[1]
                if not voce["esito_ok"]:  # errore: avvisa con un fumetto dalla tray
                    try:
                        self.icona.notify(voce["messaggio"], "File Watcher - errore")
                    except Exception:
                        pass
            elif tipo == "esci":
                self.esci()
                return
        self.root.after(300, self.leggi_coda)

    def mostra(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def nascondi(self):
        self.root.withdraw()

    def esci(self):
        self.stop.set()
        self.icona.stop()
        self.root.destroy()

    def cambia_pausa(self):
        self.stato["in_pausa"] = self.var_pausa.get()
        self.icona.update_menu()  # aggiorna la spunta nel menu della tray

    # ---------- impostazioni e ripeti ----------
    def apri_impostazioni(self):
        FinestraImpostazioni(self)

    def applica_config(self, nuova):
        """Chiamata dalla finestra impostazioni quando si preme Salva."""
        self.stato["config"] = nuova
        scrivi_json(CONFIG_PATH, nuova)
        PORTE_MAPPATE.clear()  # i parametri stampante potrebbero essere cambiati

    def ripeti(self):
        """Rilancia l'azione sul file archiviato della riga selezionata."""
        selezione = self.tabella.selection()
        if not selezione:
            messagebox.showinfo("Ripeti", "Seleziona prima una riga dalla lista.")
            return
        voce = self.voci_per_riga[selezione[0]]

        # Cerca la regola con lo stesso nome (potrebbe essere stata rinominata)
        regola = next((r for r in self.stato["config"]["regole"]
                       if r["nome"] == voce["regola"]), None)
        if regola is None:
            messagebox.showerror("Ripeti", "La regola '%s' non esiste più." % voce["regola"])
            return
        if not os.path.isfile(voce["archivio"]):
            messagebox.showerror("Ripeti", "La copia archiviata del file non è più disponibile.")
            return

        # Lo facciamo in un thread per non bloccare la finestra durante la stampa
        threading.Thread(target=self._ripeti_in_thread, args=(regola, voce), daemon=True).start()

    def _ripeti_in_thread(self, regola, voce):
        ok, messaggio = esegui_azione(regola, voce["archivio"])
        registra(nuova_voce(regola["nome"], voce["file"], ok,
                            "(ripetizione) " + messaggio, voce["archivio"]), self.coda_ui)


# ======================================================================
# 6. AVVIO
# ======================================================================

def istanza_unica():
    """Crea un mutex di Windows con nome: se esiste già, l'app è già aperta."""
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, "FileWatcher_SingleInstance")
    if kernel32.GetLastError() == 183:  # 183 = ERROR_ALREADY_EXISTS
        return None
    return handle  # va tenuto in una variabile finché l'app è aperta



def main():
    # Su Windows evita che l'interfaccia risulti sfocata sugli schermi ad alta risoluzione
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    blocco = istanza_unica()
    if blocco is None:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("File Watcher", "L'applicazione è già in esecuzione (cerca l'icona vicino all'orologio).")
        return

    app = App()
    app.root.mainloop()


if __name__ == "__main__":
    main()
