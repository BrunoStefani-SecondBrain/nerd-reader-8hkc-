# NERD Reader 🦞

Leitor de RSS pessoal, estilo Feedly — hospedado de graça no GitHub Pages.

**Ler:** https://brunostefani-secondbrain.github.io/nerd-reader-8hkc-/

## Como funciona

- A cada **30 minutos**, o GitHub Actions busca os feeds do `feeds.opml`,
  sanitiza o conteúdo e republica o site (pasta `_site`, via `build_static.py`).
- **Lido/não lido e salvos (⭐) ficam somente no seu navegador** (localStorage) —
  nada de leitura pessoal é publicado.
- Se um feed sair do ar, os artigos já vistos são preservados (histórico em cache).

## Gerenciar feeds

Edite o arquivo [`feeds.opml`](feeds.opml) aqui pelo GitHub (lápis ✏️ → commit).
A mudança entra no ar na próxima atualização (ou rode **Actions → Atualizar feeds
e publicar → Run workflow** para publicar na hora).

## Rodar localmente (opcional)

O `server.py` é um app completo e independente — com ele os feeds são buscados
da sua própria máquina:

```bash
python3 server.py            # abre em http://localhost:8484
```

---
🦞 Parte do Segundo Cérebro do Bruno. Site sem indexação (noindex).
