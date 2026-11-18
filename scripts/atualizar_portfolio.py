#!/usr/bin/env python3
"""
Sincroniza o portfólio com a pasta do Google Drive.

- Vídeo novo na pasta  -> baixa, comprime (720p H.264), gera capa e adiciona no site
- Vídeo apagado da pasta -> sai do site
- Categoria pelo nome do arquivo:
    "Publi - Nome"     -> Publi
    "Negócios - Nome"  -> Negócios
    "Saúde - Nome"     -> Saúde & Estética
    qualquer outro nome -> Saúde & Estética

Variáveis de ambiente: GOOGLE_API_KEY (obrigatória), DRIVE_FOLDER_ID.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
MANIFESTO = RAIZ / "videos.json"
PASTA_VIDEOS = RAIZ / "videos"
PASTA_CAPAS = RAIZ / "capas"

API_KEY = os.environ.get("GOOGLE_API_KEY", "").strip()
FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "1QNJu_cV-sOI0ktSV8rG6B3rJZZrz843w").strip()

CATEGORIA_PADRAO = "Saúde & Estética"
# prefixo (sem acento, minúsculo) -> categoria
PREFIXOS = {
    "publi": "Publi",
    "publicidade": "Publi",
    "negocios": "Negócios",
    "negocio": "Negócios",
    "saude": "Saúde & Estética",
    "estetica": "Saúde & Estética",
}
SUFIXOS = ["", " II", " III", " IV", " V", " VI", " VII", " VIII", " IX", " X"]


def sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", sem_acento(s).lower()).strip("-")
    return s or "video"


def api(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


def listar_drive() -> list:
    arquivos, token = [], None
    while True:
        params = {
            "q": f"'{FOLDER_ID}' in parents and trashed = false",
            "fields": "nextPageToken, files(id, name, mimeType, createdTime, size)",
            "pageSize": "1000",
            "key": API_KEY,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if token:
            params["pageToken"] = token
        dados = api("https://www.googleapis.com/drive/v3/files?" + urllib.parse.urlencode(params))
        arquivos += dados.get("files", [])
        token = dados.get("nextPageToken")
        if not token:
            break
    return [f for f in arquivos if f.get("mimeType", "").startswith("video/")]


def baixar(file_id: str, destino: Path) -> None:
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&acknowledgeAbuse=true&key={API_KEY}"
    with urllib.request.urlopen(url, timeout=600) as r, open(destino, "wb") as f:
        while True:
            bloco = r.read(1 << 20)
            if not bloco:
                break
            f.write(bloco)


def categoria_e_titulo(nome: str):
    base = re.sub(r"\.(mov|mp4|m4v|avi|mkv|webm)$", "", nome.strip(), flags=re.I)
    base = base.strip().strip("_").strip()
    m = re.match(r"^\s*([^-–—]+?)\s*[-–—]\s*(.+)$", base)
    if m:
        chave = sem_acento(m.group(1)).lower().strip()
        if chave in PREFIXOS:
            titulo = m.group(2).strip().strip("_").strip()
            return PREFIXOS[chave], titulo or base
    # sem prefixo conhecido: título é o nome inteiro, trocando " - " por ": "
    titulo = re.sub(r"\s*[-–—]\s*", ": ", base)
    return CATEGORIA_PADRAO, titulo


def eh_hdr(caminho: Path) -> bool:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=color_transfer", "-of", "csv=p=0", str(caminho)],
        capture_output=True, text=True)
    return r.stdout.strip() in ("arib-std-b67", "smpte2084")


def comprimir(origem: Path, destino: Path) -> None:
    base_vf = "scale='if(gt(iw,ih),-2,720)':'if(gt(iw,ih),720,-2)':flags=lanczos,fps=30,format=yuv420p"
    filtros = [base_vf]
    if eh_hdr(origem):
        filtros.insert(0, "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                          "tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv," + base_vf)
    for vf in filtros:
        r = subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(origem),
             "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "25",
             "-maxrate", "1200k", "-bufsize", "2400k", "-profile:v", "high",
             "-c:a", "aac", "-b:a", "96k", "-ac", "2", "-movflags", "+faststart", str(destino)])
        if r.returncode == 0:
            return
    raise RuntimeError(f"ffmpeg falhou em {origem.name}")


def gerar_capa(video: Path, capa: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-ss", "1", "-i", str(video),
         "-frames:v", "1", "-vf", "scale=540:-2", "-q:v", "4", str(capa)], check=True)


def main() -> int:
    if not API_KEY:
        print("ERRO: falta o segredo GOOGLE_API_KEY.")
        return 1

    dados = json.loads(MANIFESTO.read_text(encoding="utf-8"))
    videos = dados["videos"]
    try:
        no_drive = listar_drive()
    except Exception as e:  # não mexe no site se o Drive não responder
        print(f"ERRO ao ler a pasta do Drive: {e}")
        return 1
    if not no_drive:
        print("A pasta do Drive voltou vazia; por segurança nada foi alterado.")
        return 1

    ids_drive = {f["id"] for f in no_drive}
    ids_site = {v["id"] for v in videos}
    mudou = False

    # 1) remover o que saiu do Drive
    for v in [v for v in videos if v["id"] not in ids_drive]:
        print(f"- Removendo: {v['titulo']}")
        for p in (PASTA_VIDEOS / f"{v['arquivo']}.mp4", PASTA_CAPAS / f"{v['arquivo']}.jpg"):
            p.unlink(missing_ok=True)
        videos.remove(v)
        mudou = True

    # 2) adicionar os novos
    novos = sorted([f for f in no_drive if f["id"] not in ids_site], key=lambda f: f["createdTime"])
    for f in novos:
        categoria, titulo = categoria_e_titulo(f["name"])
        usados = {v["titulo"] for v in videos if v["categoria"] == categoria}
        titulo = next((titulo + s for s in SUFIXOS if titulo + s not in usados), titulo)
        arquivo = f"{slug(titulo)}-{slug(f['id'])[:6]}"
        print(f"+ Adicionando: {f['name']}  ->  [{categoria}] {titulo}")
        with tempfile.TemporaryDirectory() as tmp:
            bruto = Path(tmp) / "original"
            try:
                baixar(f["id"], bruto)
                comprimir(bruto, PASTA_VIDEOS / f"{arquivo}.mp4")
                gerar_capa(PASTA_VIDEOS / f"{arquivo}.mp4", PASTA_CAPAS / f"{arquivo}.jpg")
            except Exception as e:
                print(f"  ! Não consegui processar '{f['name']}': {e}. Tento de novo na próxima execução.")
                (PASTA_VIDEOS / f"{arquivo}.mp4").unlink(missing_ok=True)
                continue
        videos.append({"id": f["id"], "arquivo": arquivo, "titulo": titulo,
                       "categoria": categoria, "adicionado": f["createdTime"]})
        if categoria not in dados["ordem_categorias"]:
            dados["ordem_categorias"].append(categoria)
        mudou = True

    if mudou:
        videos.sort(key=lambda v: v["adicionado"], reverse=True)
        MANIFESTO.write_text(json.dumps(dados, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("Portfólio atualizado.")
    else:
        print("Nada novo no Drive.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
