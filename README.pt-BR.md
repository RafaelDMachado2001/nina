# NINA

[English](README.md) · **Português (Brasil)**

Uma pequena ninja em pixel art que mora no seu desktop e deixa seus agentes de código com IA **visíveis e controláveis**.
Clique nela para abrir o [Claude Code](https://docs.claude.com/en/docs/claude-code) num terminal; deixe-a aberta e ela mostra o que seus agentes estão fazendo.

> A NINA é um projeto independente, sem vínculo nem aval da Anthropic. "Claude" e "Claude Code" são marcas da Anthropic.

<p align="center"><img src="docs/images/mascot.gif" alt="A NINA reagindo ao Claude Code: trabalhando, a ferramenta mudando, pedindo permissão, voltando a trabalhar, pronto" width="300"></p>

## O que ela faz

- **Abre o Claude Code com um clique**: numa pasta padrão, numa recente ou em qualquer pasta, em qualquer uma das suas contas do Claude.
- **Reage ao que o Claude está fazendo** (por hooks do Claude Code): olhar concentrado e uma shuriken girando enquanto ele trabalha, um balão curto com a atividade
  ("Editando...", "Rodando comando..."), olhos arregalados com balão fixo quando precisa da sua permissão e uma comemoração ao terminar. Com várias sessões abertas,
  esperando vence trabalhando, que vence ocioso.
- **Painel de agentes**: cada sessão e subagente do Claude Code em árvore, com estado, última ação, linha do tempo e conversa.
- **Grafo de agentes**: grafo ao vivo, estilo Obsidian, de sessões, subagentes, ferramentas, servidores MCP e arquivos, com pulsos que mostram o fluxo de dados
  a cada evento e um **replay** na linha do tempo.
- **Agentes gerenciados** (opcional): lance agentes do Claude por *papel*, converse com eles, interrompa, retome depois de reiniciar e aprove cada ação arriscada num diálogo. Inclui um
  **criador de agentes** que entrevista você e grava subagentes e skills do projeto, um arquivo aprovado por vez.
- **Plugins**: pequenos arquivos Python locais acrescentam alertas e itens de menu (veja abaixo).
- Interface em inglês com tradução completa para português do Brasil (`"language": "pt-BR"`); um novo idioma é só um arquivo de catálogo.

## Requisitos

- Linux com X11 ou XWayland (funciona no Ubuntu/GNOME; a NINA força a plataforma Qt `xcb` para ficar sempre no topo). Windows e macOS ainda não são suportados.
- Python 3.10+ e o [Claude Code](https://docs.claude.com/en/docs/claude-code) (`claude` no `PATH`).

## Instalação

```bash
git clone https://github.com/RafaelDMachado2001/nina.git
cd nina
./scripts/install-ubuntu.sh            # Ubuntu/Debian: bibliotecas do sistema, virtualenv, atalho no menu
# ./scripts/install-ubuntu.sh agents   # ...e o suporte a agentes gerenciados (traz o CLI do Claude Code, ~300 MB)
```

Ou, em qualquer distribuição, num ambiente virtual: `pip install -e .` (acrescente `[agents]` para agentes gerenciados e `[dev]` para o linter).
São necessárias as bibliotecas xcb do Qt (no Ubuntu: `libxcb-cursor0 libxkbcommon-x11-0`).

Inicie com `nina`, clique com o botão direito no mascote e ative **Conectar ao Claude Code** (ou rode `nina --install-hooks`).
Isso faz o merge dos hooks da NINA no `settings.json` da conta ativa: nunca sobrescreve nada, guarda um backup com data e hora, é idempotente, e
`nina --uninstall-hooks` remove só as entradas da NINA. Abra uma nova sessão do `claude` depois (`/hooks` lista o que foi carregado).

## Uso

| Ação | Resultado |
|---|---|
| Clique | Abre o Claude Code na pasta padrão (enquanto ela espera, só dispensa o alerta) |
| Duplo clique | Abre a janela de conversa ao lado dela (exige o extra `agents`; o clique simples espera um instante para distinguir os dois) |
| Arrastar | Move a NINA (a posição fica salva) |
| Botão direito | Painel de agentes, novo agente, abrir em outra pasta / com outra conta, tamanho, iniciar com o sistema, conectar ao Claude Code, cochilo, sair |

![O menu do botão direito (em inglês, o idioma padrão)](docs/images/menu.png)

Ela dorme depois de 10 minutos sem uso. Num terminal: `nina say "texto"`, `nina happy`, `nina show` e `nina sleep` falam com a instância aberta.

### Estados do mascote

![Ociosa, trabalhando, esperando permissão, pronto e dormindo](docs/images/mascot-states.png)

| Evento do Claude Code | Reação |
|---|---|
| `UserPromptSubmit` | trabalhando: olhar concentrado, shuriken girando |
| `PreToolUse` | continua trabalhando; balão curto quando a ferramenta muda (Editando, Rodando comando, Lendo, Pesquisando) |
| `Notification` | esperando: olhos arregalados, "!" amarelo, balão fixo ("Preciso da sua permissão!" ou "Tô te esperando!") |
| `PostToolUse` | uma ferramenta liberada terminou: volta a trabalhar |
| `Stop` | pronto: comemora |
| `SessionEnd` | volta ao normal |

Se uma sessão ficar 3 minutos em "trabalhando" sem eventos (por exemplo, depois de um Esc), ela volta ao normal.

### Painel de agentes e grafo

Botão direito → **Agentes**. A aba **Agentes** lista as sessões com seus subagentes; selecione uma para ver a linha do tempo e a conversa (lida do transcript que o
Claude Code guarda em disco). A aba **Grafo** desenha a NINA no centro e, ao redor, as sessões, os subagentes que cada uma lançou, as ferramentas e servidores MCP
que chamaram e os arquivos que leram (bege) ou editaram (claro): a espessura da linha é quantas vezes foi usado e as linhas antigas esmaecem.
**Ao vivo** mostra cada evento como um pulso nas arestas; desmarque para o **replay** na linha do tempo. Arraste os nós, use a roda para zoom e dê duplo clique para enquadrar tudo.

![Painel de agentes com sessões, subagentes e a linha do tempo](docs/images/agents-panel.png)

![Grafo de agentes ao vivo, com pulsos nas arestas](docs/images/agent-graph.gif)

Só aparece o que os hooks informam: delegação de sessão para subagente, ferramentas, servidores MCP e arquivos. Sessões independentes **não** conversam entre si.
Os eventos ficam num SQLite local (`~/.local/state/nina/agents.db`, 7 dias) e segredos comuns (tokens, senhas, chaves) são mascarados antes de gravar ou exibir.
O formato do transcript não é documentado pelo Claude Code e pode mudar entre versões, então ele só enriquece a tela; o estado vem sempre dos hooks.

### Agentes gerenciados (opcional)

Instale o extra (`pip install -e ".[agents]"`), depois botão direito → **Novo agente...** (ou a aba **Gerenciados** do painel).
Escolha um **papel**, uma pasta, uma conta e o que quer fazer.

![Diálogo de novo agente](docs/images/new-agent-dialog.png)

O agente roda pelo
[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview), usando o seu `claude` instalado, na conta escolhida.

**Segurança:** nada acontece sem você. Leitura (`Read`, `Glob`, `Grep`) é liberada; tudo o mais pede **Permitir / Negar** com exatamente o que será executado
(o arquivo e o conteúdo, ou o comando do shell). Escrita fora dos caminhos do papel é negada sem nem perguntar, e papéis somente leitura têm `Write`/`Edit`
desativados no próprio CLI. Atenção: regras `allow` das suas próprias configurações do Claude são avaliadas antes do diálogo da NINA.

![Conversa com um agente gerenciado](docs/images/managed-agents.png)

![Diálogo de permissão mostrando o arquivo que será gravado](docs/images/permission-dialog.png)

Papéis embutidos: Assistente geral, **Criador de agentes**, Revisor técnico, Revisor de texto, Arquiteto, Gerente de projetos e Product Owner.
Crie ou sobrescreva papéis com arquivos Markdown em `~/.config/nina/roles/<id>.md`:

```markdown
---
title: Revisor de PR
description: Revisa diffs e aponta riscos
tools: Read, Glob, Grep      # liberadas, sem perguntar
writes:                      # globs relativos à pasta do agente; vazio = sem escrita nenhuma
model: sonnet
---
Você é um revisor... (o corpo é o prompt de sistema do papel)
```

### Conversar com a NINA

Dê um duplo clique nela (ou botão direito → **Conversar com a NINA...**, ou rode `nina chat`) e uma janela de conversa abre ao lado dela.
Por trás há um agente gerenciado persistente (o papel **Assistente geral**, na sua pasta padrão e conta ativa): a conversa **continua entre execuções**,
**Nova conversa** começa outra, e ela mostra os mesmos estados de qualquer agente (shuriken girando enquanto pensa, o alerta quando precisa de você).
Leituras são pré-aprovadas; toda escrita ou comando pede **Permitir / Negar**. Use `"chat_role"` no `config.json` para outro papel.

Para um **atalho global** no GNOME rode `nina --install-shortcut` (padrão `<Super>n`; `nina --uninstall-shortcut` remove). Ele registra um atalho
personalizado que executa `nina chat`; quem captura as teclas é o desktop, então funciona também no Wayland. Se a NINA roda com outro usuário que o da sessão
gráfica, passe o comando que a alcança: `--command "sudo -n env XDG_RUNTIME_DIR=/run/user/0 /caminho/nina chat"`.

### Contas e perfis

A NINA entende o layout multi-conta comum (uma pasta de configuração do Claude por conta, em `~/.claude/accounts/<conta>`) e define o `CLAUDE_CONFIG_DIR`
explicitamente ao abrir o terminal, para uma variável de ambiente perdida nunca escolher a conta errada. Escolha a conta no menu do botão direito.
Opcionalmente dê a cada conta um **perfil** no `config.json` (veja `config.example.json`):

- `default_dir`: onde o terminal abre (o `CLAUDE.md` do projeto carrega de lá)
- `add_dirs`: pastas extras (`--add-dir`)
- `context_file`: um `.md` curto acrescentado ao prompt de sistema só dessa conta
- `permissions_file`: um JSON de permissões só dessa conta (`--settings`)

Sem pastas de conta, a NINA usa o seu `~/.claude` normal.

### Plugins

Arquivos Python em `~/.config/nina/plugins/*.py` são carregados na inicialização. Um plugin define `setup(nina)` e/ou `menu(nina, menu)` e usa uma API pequena:
`nina.say()`, `nina.set_alert()` / `get_alert()` / `clear_alert()` (um alerta "!" persistente, com balão fixo e um tratador de clique opcional),
`nina.launch()`, `nina.run_process()` (sem bloquear, com timeout) e `nina.show_text()`. O contrato completo está em `src/nina/plugins.py`.
Um plugin com defeito nunca derruba a NINA. Plugins rodam como você, então só coloque ali código em que confia. Desative com `"plugins": false`.

## Configuração

`~/.config/nina/config.json` (criado no primeiro uso; veja `config.example.json`):

| Chave | Significado |
|---|---|
| `language` | `en` (padrão), `pt-BR` ou `auto` (a partir de `$LANG`) |
| `default_dir` | Pasta aberta pelo clique (padrão: sua home) |
| `account` | Pasta da conta ativa em `~/.claude/accounts` (vazio = `~/.claude` normal) |
| `profiles` | Perfis por conta (acima) |
| `terminal` | Força um terminal (`gnome-terminal`, `kitty`...); vazio = detecta |
| `sleep_minutes` | Tempo ocioso até ela dormir |
| `scale` | Tamanho: 3, 4 ou 6 |
| `chat_role` | Papel do agente da conversa (padrão: Assistente geral) |
| `claude_bin` | Caminho do `claude` para agentes gerenciados; vazio = detecta |
| `plugins` | `false` desativa os plugins locais |

Iniciar com o sistema: botão direito → **Iniciar com o sistema** (uma entrada de autostart freedesktop).

## Desenvolvimento

```bash
pip install -e ".[agents,dev]"
python -m unittest discover -s tests -t . -v     # os testes definem QT_QPA_PLATFORM=offscreen
python -m pyflakes src tests scripts
```

As capturas e GIFs de `docs/images/` são renderizados fora da tela com **dados fictícios** por `scripts/make_screenshots.py`
(`pip install -e ".[agents,docs]"` e depois `python scripts/make_screenshots.py`); ele nunca toca na sua configuração real.

O hook é a parte crítica em latência (cerca de 25 ms por chamada) e segue três regras: nunca imprime no stdout (em alguns eventos isso vira contexto do modelo),
sempre sai com 0 e engole qualquer erro, então nunca atrapalha o Claude Code, nem com a NINA fechada. A estrutura do código está descrita no [README em inglês](README.md#development).

## Limitações conhecidas

- No Python 3.10 e 3.11 a NINA precisa de um PySide6 anterior à 6.12 (a 6.12.0 tem um bug de contagem de referências que aborta o processo); os metadados do pacote já restringem isso, e a NINA se recusa a iniciar numa combinação ruim em vez de travar depois.
- Só Linux/X11 por enquanto; em Wayland puro o comportamento é limitado pelo compositor (por isso ela roda via XWayland). Windows e macOS não estão nos planos por ora.
- Sessões observadas (abertas nos seus próprios terminais) não podem ser conduzidas pelo painel: ele é somente leitura. **Abrir no terminal** reabre a sessão com `claude --resume`, na conta em que ela rodou, e **Copiar comando de retomada** entrega o comando. Só agentes gerenciados podem ser conduzidos pela NINA.
- Um agente gerenciado encerrado, ou ainda aberto quando a NINA fechou, volta na aba **Gerenciados** como *encerrado*, e **Retomar** continua a mesma sessão do Claude. O log visível é mantido (últimas 200 linhas, em `~/.local/state/nina/managed.json`, legível só por você); pedidos de permissão pendentes não.

## Licença

[MIT](LICENSE) © 2026 Rafael Machado.
