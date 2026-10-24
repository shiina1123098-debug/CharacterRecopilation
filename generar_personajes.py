import json
import socket
import os
import re
import time
import unicodedata
from difflib import SequenceMatcher

import requests
import requests.packages.urllib3.util.connection as urllib3_cn

def _allowed_gai_family():
    return socket.AF_INET
urllib3_cn.allowed_gai_family = _allowed_gai_family

# ================== CONFIGURACIÓN ==================
MODE = os.environ.get("MODE", "generate").lower()                 # "generate" | "merge"
SORT_ORDER = os.environ.get("SORT_ORDER", "desc").lower()         # "desc" | "asc"
SOURCE = os.environ.get("SOURCE", "characters").lower()           # "characters" | "manga"

# Autodetecta rw.json en la raíz o en data/
RW_FILE = next((r for r in ["rw.json", "data/rw.json"] if os.path.exists(r)), "rw.json")

if MODE == "generate":
    OUTPUT_FILE = f"new_characters_{SOURCE}_{SORT_ORDER}.json"
    CHECKPOINT_FILE = f"checkpoint_{SOURCE}_{SORT_ORDER}.json"
else:
    OUTPUT_FILE = "new_characters.json"
    CHECKPOINT_FILE = None

PAGINAS_A_BUSCAR = int(os.environ.get("PAGINAS_A_BUSCAR", "4000"))
PAUSA_ENTRE_PAGINAS = float(os.environ.get("PAUSA_ENTRE_PAGINAS", "0.6"))
CHECKPOINT_CADA_N = 100

UMBRAL_FUZZY = 0.90
UMBRAL_FUENTE = 0.50
UMBRAL_FUENTE_PALABRAS = 0.75
# ===================================================

RANGOS_POR_FAVORITOS = [
    (0,      300,         5_000,     24_999),
    (300,    1_500,       25_000,    74_999),
    (1_500,  5_000,       75_000,    199_999),
    (5_000,  12_000,      200_000,   499_999),
    (12_000, 20_000,      500_000,   999_999),
    (20_000, float("inf"), 1_000_000, 2_500_000),
]

def calcular_valor(favoritos):
    for fav_min, fav_max, val_min, val_max in RANGOS_POR_FAVORITOS:
        if favoritos < fav_max:
            if fav_max == float("inf"):
                return val_max
            frac = (favoritos - fav_min) / (fav_max - fav_min)
            return int(val_min + (val_max - val_min) * frac)
    return 2_500_000


def normalizar_nombre(nombre):
    if not nombre:
        return ""
    nombre = unicodedata.normalize('NFD', nombre)
    nombre = ''.join(c for c in nombre if unicodedata.category(c) != 'Mn')
    nombre = nombre.lower().replace('ou', 'o').replace('uu', 'u')
    nombre = re.sub(r"[^a-z0-9\s]", ' ', nombre)
    palabras = [p for p in nombre.split() if len(p) > 1] or nombre.split()
    palabras.sort()
    return ' '.join(palabras)


def normalizar_fuente(fuente):
    if not fuente:
        return ""
    fuente = unicodedata.normalize('NFD', fuente)
    fuente = ''.join(c for c in fuente if unicodedata.category(c) != 'Mn')
    fuente = fuente.lower()
    fuente = re.sub(r'\([^)]*\)', '', fuente)
    for sufijo in [' tv', ' ona', ' ova', ' movie', ' film', ' season', ' 2', ' 3', ' 4', ' 5', ' 0']:
        if fuente.endswith(sufijo):
            fuente = fuente[:-len(sufijo)].strip()
    fuente = re.sub(r"[^a-z0-9\s]", ' ', fuente)
    return ' '.join(fuente.split())


def similitud_segura(a, b, umbral=0.0):
    if not a or not b:
        return 0.0
    len_a, len_b = len(a), len(b)
    max_posible = 2 * min(len_a, len_b) / (len_a + len_b)
    if max_posible < umbral:
        return max_posible
    return SequenceMatcher(None, a, b).ratio()


class DetectorDuplicados:
    UMBRAL_FUENTE_PALABRAS = UMBRAL_FUENTE_PALABRAS

    def __init__(self, personajes):
        self.por_nombre = {}
        self.por_nombre_fuente = {}
        self.por_letra = {}
        self.por_palabra = {}
        for p in personajes:
            self.agregar(p)

    def agregar(self, p):
        nombre = p.get("Nombre") or p.get("nombre") or p.get("name") or ""
        fuente = p.get("Fuente") or p.get("fuente") or p.get("source") or ""
        n_norm = normalizar_nombre(nombre)
        s_norm = normalizar_fuente(fuente)
        if not n_norm:
            return
        self.por_nombre.setdefault(n_norm, []).append(p)
        self.por_nombre_fuente[(n_norm, s_norm)] = p
        self.por_letra.setdefault(n_norm[0], {}).setdefault(n_norm, []).append(p)
        for palabra in n_norm.split():
            if len(palabra) >= 3:
                self.por_palabra.setdefault(palabra, {}).setdefault(n_norm, []).append(p)

    def _candidatos_por_palabras(self, palabras):
        resultado = {}
        for palabra in palabras:
            for nombre_norm, entradas in self.por_palabra.get(palabra, {}).items():
                if nombre_norm not in resultado:
                    resultado[nombre_norm] = entradas
        return resultado

    def es_duplicado(self, nombre, fuente):
        n_norm = normalizar_nombre(nombre)
        s_norm = normalizar_fuente(fuente)
        if not n_norm:
            return False, ""

        if (n_norm, s_norm) in self.por_nombre_fuente:
            return True, "mismo nombre y fuente"

        if n_norm in self.por_nombre:
            e = self.por_nombre[n_norm][0]
            return True, f"mismo nombre ('{e.get('Nombre')}' de '{e.get('Fuente')}')"

        candidatos = self.por_letra.get(n_norm[0], {})

        mejor_ratio = 0.0
        mejor_entrada = None
        for nombre_existente, entradas in candidatos.items():
            ratio = similitud_segura(n_norm, nombre_existente, UMBRAL_FUZZY)
            if ratio >= UMBRAL_FUZZY and ratio > mejor_ratio:
                mejor_ratio = ratio
                mejor_entrada = entradas[0]

        if mejor_entrada is not None:
            nom_ex = mejor_entrada.get("Nombre") or "?"
            fuen_ex = mejor_entrada.get("Fuente") or "?"
            s_ex = normalizar_fuente(fuen_ex)
            if similitud_segura(s_norm, s_ex, UMBRAL_FUENTE) >= UMBRAL_FUENTE:
                return True, f"nombre similar ({mejor_ratio:.0%}): '{nom_ex}' de '{fuen_ex}'"
            elif mejor_ratio >= 0.97:
                return True, f"casi idéntico ({mejor_ratio:.0%}): '{nom_ex}' de '{fuen_ex}'"

        palabras_nuevas_todas = set(n_norm.split())
        palabras_nuevas = {p for p in palabras_nuevas_todas if len(p) >= 3}
        if not palabras_nuevas:
            return False, ""

        for nombre_existente, entradas in self._candidatos_por_palabras(palabras_nuevas).items():
            if nombre_existente == n_norm:
                continue
            palabras_ex_todas = set(nombre_existente.split())
            palabras_ex = {p for p in palabras_ex_todas if len(p) >= 3}

            if (palabras_nuevas_todas.issubset(palabras_ex_todas)
                    or palabras_ex_todas.issubset(palabras_nuevas_todas)):
                for entrada in entradas:
                    fuen_ex = entrada.get("Fuente") or "?"
                    if similitud_segura(s_norm, normalizar_fuente(fuen_ex), 0.4) >= 0.4:
                        return True, f"palabras contenidas: '{entrada.get('Nombre')}' de '{fuen_ex}'"

            comunes = palabras_nuevas & palabras_ex
            if comunes:
                for entrada in entradas:
                    fuen_ex = entrada.get("Fuente") or "?"
                    if similitud_segura(s_norm, normalizar_fuente(fuen_ex), self.UMBRAL_FUENTE_PALABRAS) >= self.UMBRAL_FUENTE_PALABRAS:
                        return True, f"palabras en común ({', '.join(sorted(comunes))}): '{entrada.get('Nombre')}' de '{fuen_ex}'"

        return False, ""


def cargar_checkpoint():
    if CHECKPOINT_FILE and os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            print(f"💾 Checkpoint: continuando desde pág {data['next_page']}, {len(data['nuevos'])} acumulados")
            return data
        except Exception as e:
            print(f"⚠️ Checkpoint corrupto, empezando de cero: {e}")
    return {"next_page": 1, "nuevos": []}


def guardar_checkpoint(next_page, nuevos):
    if CHECKPOINT_FILE:
        with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
            json.dump({"next_page": next_page, "nuevos": nuevos}, f, ensure_ascii=False)


ANILIST_URL = "https://graphql.anilist.co"


def construir_query_characters(sort_order):
    """Query sobre personajes ordenados por favoritos (mezcla cualquier formato)."""
    sort_clause = "FAVOURITES" if sort_order == "asc" else "FAVOURITES_DESC"
    return f"""
query ($page: Int) {{
  Page(page: $page, perPage: 50) {{
    characters(sort: {sort_clause}) {{
      name {{ full }}
      favourites
      gender
      image {{ large }}
      media(perPage: 1) {{ nodes {{ title {{ romaji }} }} }}
    }}
  }}
}}
"""


def construir_query_manga():
    """
    Query sobre MEDIA de tipo MANGA con format_in que cubre manhwa/manhua/manga.
    Trae hasta 20 series por página y, de cada una, hasta 25 personajes.
    O sea ~500 personajes por request (más generoso que la query de characters).
    """
    return """
query ($page: Int) {
  Page(page: $page, perPage: 20) {
    media(type: MANGA, format_in: [MANHWA, MANHUA, MANGA], sort: POPULARITY_DESC) {
      id
      title { romaji english }
      characters(page: 1, perPage: 25, sort: [ROLE, RELEVANCE, ID]) {
        nodes {
          name { full }
          favourites
          gender
          image { large }
        }
      }
    }
  }
}
"""


def fetch_pagina(page, query, reintentos=5):
    for intento in range(reintentos):
        try:
            r = requests.post(ANILIST_URL, json={"query": query, "variables": {"page": page}}, timeout=45)
            if r.status_code == 429:
                espera = 60 * (intento + 1)
                print(f"⏸️ 429 pág {page}, esperando {espera}s")
                time.sleep(espera)
                continue
            r.raise_for_status()
            page_data = r.json().get("data", {}).get("Page", {})
            # Devuelve el Page completo (puede tener .characters o .media)
            return page_data
        except requests.exceptions.ConnectionError as e:
            print(f"❌ Sin conexión pág {page}: {e}")
            time.sleep(5)
        except requests.exceptions.Timeout:
            print(f"⏱️ Timeout pág {page}")
            time.sleep(5)
        except Exception as e:
            print(f"❌ Error pág {page}: {e}")
            time.sleep(3)
    return None


def cargar_personajes_existentes(ruta):
    if not os.path.exists(ruta):
        print(f"⚠️ No encontré {ruta}. Base vacía.")
        return []
    with open(ruta, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list) and data:
        print(f"🔑 Claves: {list(data[0].keys())}")
    return data if isinstance(data, list) else []


def mapear_genero(g):
    if not g:
        return "o"
    g = g.lower()
    return "f" if g == "female" else ("m" if g == "male" else "o")


def extraer_personajes_de_pagina(page_data):
    """
    Devuelve una lista uniforme de tuplas (nombre, fuente, favoritos, genero, imagen)
    a partir del Page data, sea de characters (SOURCE=characters) o de media (SOURCE=manga).
    """
    if not page_data:
        return []

    # SOURCE=characters: viene en page_data["characters"]
    if "characters" in page_data and page_data["characters"]:
        out = []
        for pj in page_data["characters"]:
            nombre = (pj.get("name") or {}).get("full", "").strip()
            if not nombre:
                continue
            media_nodes = (pj.get("media") or {}).get("nodes") or []
            fuente = "Desconocida"
            if media_nodes:
                titulo = media_nodes[0].get("title") or {}
                fuente = titulo.get("romaji") or titulo.get("english") or titulo.get("native") or "Desconocida"
            out.append((
                nombre,
                fuente,
                pj.get("favourites") or 0,
                mapear_genero(pj.get("gender")),
                (pj.get("image") or {}).get("large", ""),
            ))
        return out

    # SOURCE=manga: viene en page_data["media"], cada media tiene .characters.nodes
    if "media" in page_data and page_data["media"]:
        out = []
        for media in page_data["media"]:
            titulo = media.get("title") or {}
            fuente = titulo.get("romaji") or titulo.get("english") or "Desconocida"
            chars = (media.get("characters") or {}).get("nodes") or []
            for pj in chars:
                nombre = (pj.get("name") or {}).get("full", "").strip()
                if not nombre:
                    continue
                out.append((
                    nombre,
                    fuente,
                    pj.get("favourites") or 0,
                    mapear_genero(pj.get("gender")),
                    (pj.get("image") or {}).get("large", ""),
                ))
        return out

    return []


def modo_generate():
    print(f"🚀 GENERATE (source={SOURCE}, sort={SORT_ORDER}, hasta {PAGINAS_A_BUSCAR} págs)\n")
    print(f"📂 rw.json: {RW_FILE}")
    print(f"📂 Salida: {OUTPUT_FILE}")
    print(f"📂 Checkpoint: {CHECKPOINT_FILE}\n")

    existentes = cargar_personajes_existentes(RW_FILE)
    print(f"✅ Personajes en rw.json: {len(existentes):,}")

    cp = cargar_checkpoint()
    page_inicial = cp["next_page"]
    nuevos = cp["nuevos"]

    print("📇 Construyendo índice...")
    t0 = time.time()
    detector = DetectorDuplicados(existentes + nuevos)
    print(f"   {len(detector.por_nombre):,} nombres únicos, {len(detector.por_palabra):,} palabras en {time.time()-t0:.1f}s\n")

    if page_inicial > 1:
        print(f"⏩ Retomando desde pág {page_inicial}\n")

    if SOURCE == "manga":
        query = construir_query_manga()
    else:
        query = construir_query_characters(SORT_ORDER)

    duplicados = 0
    vacias_seguidas = 0
    inicio = time.time()
    total_inicio = len(nuevos)

    for page in range(page_inicial, PAGINAS_A_BUSCAR + 1):
        page_data = fetch_pagina(page, query)
        if page_data is None:
            print(f"⚠️ Pág {page} falló, salteando")
            continue

        personajes_pagina = extraer_personajes_de_pagina(page_data)

        if not personajes_pagina:
            vacias_seguidas += 1
            print(f"📭 Pág {page} vacía ({vacias_seguidas} seguidas)")
            if vacias_seguidas >= 5:
                print("🛑 5 vacías seguidas → cortando")
                break
            continue
        vacias_seguidas = 0

        for nombre, fuente, favoritos, genero, imagen in personajes_pagina:
            es_dup, _ = detector.es_duplicado(nombre, fuente)
            if es_dup:
                duplicados += 1
                continue

            nuevo = {
                "Nombre": nombre,
                "Imagen": imagen,
                "Fuente": fuente,
                "Valor": calcular_valor(favoritos),
                "Genero": genero,
            }
            nuevos.append(nuevo)
            detector.agregar(nuevo)
            print(f"   ✅ {nombre} | {favoritos:,} fav | {nuevo['Valor']:,}")

        guardar_checkpoint(page + 1, nuevos)

        transcurrido = time.time() - inicio
        nuevos_corrida = len(nuevos) - total_inicio
        vel = (page - page_inicial + 1) / transcurrido if transcurrido > 0 else 0
        eta = (PAGINAS_A_BUSCAR - page) / vel if vel > 0 else 0
        print(f"📄 Pág {page:>4}/{PAGINAS_A_BUSCAR} | nuevos: {nuevos_corrida:>4} | dup: {duplicados:>6} | {vel:.2f} pág/s | ETA: {eta/60:.1f} min")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(nuevos, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print(f"🎉 FIN. {len(nuevos):,} nuevos en '{OUTPUT_FILE}'")
    print(f"⏭️  Dup: {duplicados:,} | ⏱️ {(time.time()-inicio)/60:.1f} min")


def modo_merge():
    print("🔀 MERGE\n")
    rw = cargar_personajes_existentes(RW_FILE)
    print(f"✅ rw.json: {len(rw):,}")

    detector = DetectorDuplicados(rw)
    final = []

    # Busca TODOS los archivos de resultado que haya dejado cada job
    archivos = [
        "new_characters_characters_desc.json",
        "new_characters_characters_asc.json",
        "new_characters_manga_desc.json",
        "new_characters_manga_asc.json",
    ]

    for ruta in archivos:
        if not os.path.exists(ruta):
            print(f"⚠️ No existe {ruta}, salteando")
            continue
        with open(ruta, "r", encoding="utf-8") as f:
            items = json.load(f)
        print(f"\n📥 {ruta}: {len(items):,}")
        agregados = dup = 0
        for item in items:
            es_dup, _ = detector.es_duplicado(item.get("Nombre", ""), item.get("Fuente", ""))
            if es_dup:
                dup += 1
                continue
            final.append(item)
            detector.agregar(item)
            agregados += 1
        print(f"   ✅ Únicos: {agregados:,} | ⏭️ Dup: {dup:,}")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(final, f, ensure_ascii=False, indent=2)
    print(f"\n🎉 Total: {len(final):,} personajes en '{OUTPUT_FILE}'")


if __name__ == "__main__":
    if MODE == "merge":
        modo_merge()
    else:
        modo_generate()
