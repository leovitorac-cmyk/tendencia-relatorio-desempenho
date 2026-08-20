# Relatório de Desempenho — Monitoramento por UC

Monitora a caixa Gmail `awstendenciaenergiatendenciaen@gmail.com` (apelido interno "aws"), identifica relatórios de desempenho que chegam por email, extrai as UCs cobertas por cada anexo e registra no Supabase. Nos dias 15, 20, 22, 25 e todo dia até o fim do mês, verifica quais UCs ainda não têm relatório do **mês anterior** (relatório de desempenho é sempre referente ao mês anterior — em agosto verifica julho) e notifica o gestor responsável (email via Brevo) + cria uma Solicitação no CRM Bubble. Coordenadores (Felipe e Diogo) recebem também um e-mail consolidado com as pendências de todos os gestores.

> **Status (2026-08-20): primeiro disparo real de produção rodado.** Dia 20 (checagem com Solicitação), ambiente Live: 8 gestores notificados, 448 Solicitações criadas, 2 consolidados enviados aos coordenadores. Detectado e corrigido no mesmo dia um lote de falsos positivos (31 UCs/25 clientes, em todos os 8 gestores) causado por bugs de extração — Solicitações erradas arquivadas + email de correção reenviado. Estado final julho/2026: 388 recebidos / 422 pendentes reais de 810 UCs. Ver "Bugs de matching corrigidos (2026-08-20)" abaixo pro histórico técnico completo. **Ainda roda manualmente** (Leo/Claude no terminal) — automação via GitHub Actions é o próximo passo, plano em `/Users/leonardovitortoschiamorim/.claude/plans/temos-2-frentes-as-squishy-ullman.md`. Detalhes em `[[project_relatorio_desempenho]]` (memória Claude).

> **Sem dependência do módulo Simple/UNO.** Não usa `Receitas` do Bubble, não chama nenhuma Edge Function do motor de auditoria. Compartilha só o **projeto** Supabase com o simple-uno (`czqvenpkfsbepggdktfn`), mas em tabelas próprias (`ucs_gestor`, `relatorios_recebidos`), sem tocar nas tabelas do motor.

## Fluxo

1. **Planilha de UCs + gestor** (fornecida pelo Leo, formato livre — colunas mapeadas manualmente por sessão) → tabela `ucs_gestor`. Última importação: 810 UCs únicas, 8 gestores (mapa completo abaixo). `import_ucs_gestor.py` existe pra reimportar via CSV padronizado (`uc_codigo,cliente_nome,gestor_nome,gestor_email,gestor_bubble_user_id`) se a planilha vier nesse formato de novo.
2. **`watch_relatorio_desempenho.py`** (rodar manualmente por enquanto, ver "Status" acima) — lê a caixa "aws", baixa anexos de relatório, extrai identificações (UC + nome + CNPJ, `extractor.py`) + mês/ano via assunto do email (não do PDF), casa cada UC contra `ucs_gestor` numa cascata de 5 níveis (ver seção própria abaixo) e faz upsert em `relatorios_recebidos` com `recebido=true` usando sempre o `uc_codigo` **canônico** (o que já está cadastrado), nunca o que veio cru do PDF.
3. **`check_relatorios_pendentes.py`** (rodar manualmente, age só nos dias `DIAS_CHECAGEM = {15,20,22,25,26,27,28,29,30,31}`) — cruza `ucs_gestor` × `relatorios_recebidos` do **mês anterior** ao atual:
   - UC sem `recebido=true` pro mês de referência → pendente.
   - Pra cada gestor com pendência: 1 e-mail (Brevo) sempre. `Solicitacao` no Bubble (1 por UC pendente) **só no dia 20** — nos outros dias de checagem é só o e-mail, sem card no CRM (decisão do Leo, 2026-08-19).
   - Solicitação criada com `data entrega` = último dia do mês corrente (não do mês de referência do relatório), `Responsavel_Pessoa` = `gestor_bubble_user_id`, título liderando com nome do cliente. **Não** marca `veio_nexi` (bug corrigido 2026-08-20 — antes marcava por engano, fazia o card mostrar tag "Nexi" indevida).
   - Pra cada coordenador (`COORDENADORES` — Felipe e Diogo): 1 e-mail consolidado com todas as pendências de todos os gestores, separado por seção. Diogo também recebe o individual dele (é gestor — recebe as UCs em branco da planilha).
   - Dedupe: `notificado_em` gravado em `relatorios_recebidos` evita notificar a mesma UC duas vezes no mesmo ciclo de checagem.

## Cascata de matching (`watch_relatorio_desempenho.py::known_uc_codes()`)

5 níveis, cada um só tenta se o anterior falhou, sempre gravando o `uc_codigo` **canônico** de `ucs_gestor` (nunca o valor cru do PDF):

1. **Código exato** — bate direto com `ucs_gestor.uc_codigo`.
2. **Parte de código composto** — ~106 UCs em `ucs_gestor` têm 2 identificadores separados por `/` (`9002458/9002458` repetido, ou `2071746/58248307` — 2 sistemas diferentes); PDF só traz 1 dos lados.
3. **Só-dígitos** — PDF traz pontuação diferente da planilha (`558.598.053-10` vs `55859805310`).
4. **Razão social da linha** (`match_por_nome`, score mín. 0.6) — distribuidora renumerou a conta e o PDF traz um código totalmente diferente do cadastrado (não é formatação, é outro número mesmo). Jaccard de tokens sobre `cliente_nome`, ignorando stopwords tipo LTDA/DE/DA.
5. **Nome do cliente tirado do ASSUNTO do email** (`match_por_nome_grupo`, score mín. 0.75) — último recurso, pra cliente com várias UCs/filiais onde cada linha do PDF é identificada pelo nome da FILIAL ("AV SAUDADE", "EINHELL"), não da empresa. Sem CNPJ por UC no cadastro pra saber qual linha é qual filial, credita **todas** as UCs cadastradas da empresa quando o nome do assunto bate — trade-off aceito pelo Leo (prefere super-creditar a marcar pendente quem claramente mandou o relatório).

Só quando os 5 níveis falham a UC vira "desconhecida" (logada, não gravada).

### Bugs de matching corrigidos (2026-08-20)

Todos descobertos e corrigidos no mesmo dia do primeiro disparo real, depois que o Leo reportou clientes notificados por engano:

- **Código composto sem match** (`extractor.py`/`watch_relatorio_desempenho.py`): nível 2 acima, não existia até esta data.
- **Distribuidora renumerou a conta** (TREVO, GAUCHA DE PESCA, SCAPOL): nível 4 acima.
- **Cliente multi-UC identificado por filial, não por razão social** (ANCORA GROUP — linhas "AV SAUDADE"/"EINHELL"): nível 5 acima.
- **Código colado no nome da cidade sem espaço** (`extractor.py::_strip_glued_city_suffix`) — PDF às vezes traz `"204803095-VARGEM GRANDE PAULISTA"` ou `"200964261-COTIA"` (hífen sem espaço em volta, diferente de dígito verificador tipo `"0616541-9"`). Cidade de 1 palavra ainda casava por acidente via fallback só-dígitos; cidade de 2+ palavras fazia a linha inteira ser descartada **sem log nenhum** — pior caso, UC sumia silenciosamente. Esse foi o bug que afetou VIBROPAC (gestor Victor Julio) e, no rescan geral, mais **31 UCs / 25 clientes em todos os 8 gestores**.
- **Consequência do lote de bugs:** 31 Solicitações criadas incorretamente no disparo real de 2026-08-20 (clientes que já tinham mandado relatório) — arquivadas no Bubble Live com nota explicativa + e-mail de correção reenviado a cada gestor afetado + consolidado corrigido aos coordenadores. Script de reprocessamento local usado pra recuperar tudo sem precisar rebaixar do Gmail: reparsa os PDFs já salvos em `email/downloads/`, roda a cascata atualizada, faz upsert direto — útil de novo se aparecer mais um bug de extração no futuro (não versionado no repo, foi um script de sessão).

## Template de e-mail

`email_template()` em `check_relatorios_pendentes.py` — mesma identidade visual do template usado em `crm/solicitacoes/automacao/cobranca_diaria.py` (header azul petróleo `#0B4A63`, selo verde, cards brancos por UC). Preview visual aprovado pelo Leo em 2026-08-11.

## Tabelas Supabase (`czqvenpkfsbepggdktfn`, projeto compartilhado com simple-uno)

- `ucs_gestor`: `uc_codigo` (chave), `cliente_nome`, `gestor_nome`, `gestor_email`, `gestor_bubble_user_id` (opcional), `ativo`.
- `relatorios_recebidos`: `uc_codigo`, `mes`, `ano`, `recebido`, `data_recebimento`, `gmail_message_id`, `notificado_em` (dedupe de notificação), `solicitacao_bubble_id`.
- `justificativa_ciclos` (`migration_justificativa.sql`): 1 linha por gestor por ciclo mensal de justificativa — `mes`, `ano`, `gestor_email`, `gestor_nome`, `token`, `total_itens`, `solicitacao_bubble_id`, `analise_enviada`. Único `(mes,ano,gestor_email)` — garante idempotência do `enviar_justificativa_atraso.py`.
- `justificativa_itens` (`migration_justificativa_itens.sql`): 1 linha por UC pendente dentro de um ciclo — `uc_codigo`, `cliente_nome`, `gestor_nome`, `gestor_email`, `mes`, `ano`, `token` (FK pra `justificativa_ciclos.token`), `motivo`, `respondido`, `respondido_em`. **Substitui o Data Type `JustificativaAtraso` que seria criado no Bubble** (decisão do Leo em 2026-08-17: fica tudo no Supabase pra facilitar cruzar com `ucs_gestor`/`relatorios_recebidos`). RLS restringe leitura/escrita pública ao dono do token via header `x-token` — ver comentário no topo da migration.

## Fluxo de justificativa de atraso (1o dia útil do mês)

Fecha o ciclo de checagem do mês: cada UC que ficou o mês inteiro sem
relatório vira uma pendência de **justificativa formal** do gestor, e só
depois de todos os gestores responderem os coordenadores recebem a análise.

1. **`enviar_justificativa_atraso.py`** (cron 1x, dia 1 útil do mês) — busca
   UCs com `recebido=false` do mês de referência (2 meses atrás — em 01/09
   fecha julho, `mes=7`). Por gestor com pendência: gera `token`, upserta 1
   linha em `justificativa_ciclos`, cria 1 linha por UC em `justificativa_itens`
   (Supabase), 1 `Solicitacao` no Bubble (prazo = próximo dia útil,
   `Responsavel_Pessoa=gestor_bubble_user_id`, `Criador`=user Automação) e
   manda e-mail (Brevo) com o link
   `plataforma.tendenciaenergia.com.br/justificar-atraso?token=...`.
2. **Página Bubble `justificar-atraso`** (no-code, sem login — token na URL
   autentica) — **não usa Data Type do Bubble.** A página só tem 1 elemento
   HTML apontando pro registro `Nome = "Justificativa Atraso"` da Data Type
   `Claude` (mesmo padrão do `fechamento-mensal`/CRM — ver `crm/CLAUDE.md`).
   O conteúdo real (fonte de verdade versionada) é
   `simple/relatorio-desempenho/justificar-atraso.html`: JS puro que lê
   `token` da URL e conversa DIRETO com o Supabase REST (`justificativa_itens`),
   sem Bubble Data API — mostra 1 UC pendente por vez, dropdown com os 7
   motivos, grava `motivo`/`respondido=true`/`respondido_em` a cada
   "Concluir" e avança sozinho. Segurança fica na RLS da tabela (header
   `x-token` tem que bater com o `token` da linha — ver
   `migration_justificativa_itens.sql`), não em Option Set/Privacy Rule do
   Bubble.
   - **Sincronizar após qualquer ajuste:**
     `python3 sync_html_claude.py justificar-atraso.html "Justificativa Atraso" --env test --env live`
3. **`enviar_analise_atrasos.py`** (cron diário) — pra cada ciclo `(mes,ano)`
   com `analise_enviada=false`: checa no **Supabase** (`justificativa_itens`,
   não Bubble) se todo gestor do ciclo já respondeu todas as UCs
   (`respondido=false` count=0 por `token`). Só quando **todos** fecharam:
   tabula motivo mais comum + ranking de gestor com mais atraso, manda 1
   e-mail (Brevo) consolidado pra Felipe e Diogo, marca `analise_enviada=true`.

### Bubble — página `justificar-atraso`

Não precisa de Option Set nem Data Type novos (decisão 2026-08-17 — ver
seção "Fluxo" acima). Único pré-requisito no editor Bubble: 1 página
`justificar-atraso` com parâmetro de URL `token` e 1 elemento HTML com
"source" = `Search Claude (Nome = "Justificativa Atraso") :first item's Html`.
Confirmado criado pelo Leo em 2026-08-17 (registro `Claude` já existe nos
2 ambientes, sincronizado por `sync_html_claude.py`).

## Mapa de gestor (nome curto da planilha → usuário Bubble completo)

Confirmado idêntico entre ambiente Test e Live:

| gestor_nome | gestor_email |
|---|---|
| Mickela Moriconi | mickela.moriconi@tendenciaenergia.com.br |
| Alexandre Santos | alexandre.santos@tendenciaenergia.com.br |
| Renata Sampaio | renata.sampaio@tendenciaenergia.com.br |
| Guilherme Staudt | guilherme.staudt@tendenciaenergia.com.br |
| Victor Julio | victor.julio@tendenciaenergia.com.br |
| Matheus Vendrame | matheus.vendrame@tendenciaenergia.com.br |
| Eliane Ambrosio | eliane.ambrosio@tendenciaenergia.com.br |
| Diogo Tavares (default p/ UC sem gestor definido na planilha) | diogo.tavares@tendenciaenergia.com.br |

Coordenadores (`COORDENADORES` em `check_relatorios_pendentes.py`, hardcoded): Felipe (`felipe@tendenciaenergia.com.br`) e Diogo Tavares.

## Setup necessário

- `.env.local`: `BUBBLE_API_KEY_TEST`/`_LIVE` ✅, `SUPABASE_RELATORIO_URL`/`_SERVICE_KEY` ✅, `BREVO_API_KEY` + remetente ✅, `BUBBLE_AUTOMACAO_USER_ID_TEST`/`_LIVE` ✅ (resolvido 2026-08-17 — user "Automação" criado pelo Leo).
- `email/client_secret.json` + `email/token.json` — OAuth Desktop próprio da conta "aws" (escopo `gmail.readonly`) ✅ já gerado.
- **Pendente rodar `migration_justificativa_itens.sql` no SQL Editor do Supabase** (projeto `czqvenpkfsbepggdktfn`) — Claude não tem credencial de DDL nesse projeto (só a service key REST, que não executa `CREATE TABLE`/RLS). Leo precisa colar e rodar uma vez — depois disso os scripts funcionam sem mais nada manual.
- Crontabs/GitHub Actions ainda não configurados — **primeiro disparo real (2026-08-20) foi manual**, rodado no terminal. Próximo passo combinado com o Leo: automação via GitHub Actions, repo novo dedicado só a este módulo (não o workspace Tendencia inteiro — evita subir dados de cliente/financeiro/wiki). Plano detalhado (passo a passo, secrets necessários, risco de expiração do token OAuth em modo "Testing" do Google Cloud Console) em `/Users/leonardovitortoschiamorim/.claude/plans/temos-2-frentes-as-squishy-ullman.md`.
- `.github/workflows/relatorio-desempenho.yml` já existe no disco com os 3 steps de notificação/justificativa, mas **não cobre o watcher** (`watch_relatorio_desempenho.py`) nem foi testado em CI de verdade — precisa dos ajustes do plano acima (credenciais Gmail como secrets, completar `requirements.txt` com `pdfplumber`/`openpyxl`/`google-auth*`, copiar `gmail_client.py` pro repo isolado).

## Open items

- `gestor_bubble_user_id`: se uma UC não tiver esse ID, a `Solicitacao` é criada sem `Responsavel_Pessoa` estruturado (nome fica só no Título/Descrição). Hoje todas as 810 UCs importadas têm o ID preenchido.
- Critério exato de busca do email (assunto/remetente) e formato do anexo — validado contra emails reais em 2026-08-10, mas ajustar em `watch_relatorio_desempenho.py`/`extractor.py` se aparecer formato novo.
- Fluxo de justificativa: código atualizado 2026-08-17 pra usar Supabase (`justificativa_itens`) em vez de Data Type Bubble, e página `justificar-atraso.html` sincronizada nos 2 ambientes via `sync_html_claude.py`. **Falta:** rodar `migration_justificativa_itens.sql` no Supabase (bloqueia tudo — sem a tabela a página e os scripts quebram) e o Leo reavaliar o preview do email + da página antes de qualquer dry-run real. Nenhum teste rodado ainda (combinado 2026-08-17).
- 422 UCs pendentes de 07/2026 após o disparo real + correções de 2026-08-20 (número final, ver "Status" acima). Refletido no Bubble Live (Solicitações) e nos e-mails já enviados.
- **Fallback por CNPJ ainda não existe** — `ucs_gestor` não tem coluna de CNPJ hoje (só `uc_codigo`/`cliente_nome`/`gestor_*`). `extractor.py` já extrai o CNPJ de cada linha do PDF (campo `identificacoes[].cnpj`) mas ele não é usado no matching ainda. Se quiser esse fallback (mais preciso que nome pra desambiguar filial), precisa: Leo decidir a fonte (Bubble `Clientes.CNPJ_Norm` via API, ou nova coluna preenchida manualmente) + eu adicionar a coluna e o nível de matching.
- **Nível 5 da cascata (nome do assunto) pode super-creditar**: quando um cliente multi-UC/filial só manda relatório de 1 filial mas o assunto bate com a empresa toda, credita todas as UCs cadastradas dela — decisão consciente (Leo, 2026-08-20) de preferir isso a marcar pendente quem claramente mandou algo. Não auditado quantas vezes isso pode ter super-creditado (ex.: filial fechou, não deveria mais receber relatório).
- **Tag "Automação" no card do CRM — bloqueada, precisa ação do Leo.** Pedido: Solicitação criada por esta automação deveria ter uma tag tipo "Nexi"/"Tendência". Confirmado no schema real do Bubble (Test) que essas tags **não são um campo genérico** — são regras fixas no front-end do CRM (`crm/solicitacoes/mockup/app.js` linhas ~206-207): `veio_nexi` (yes/no) → tag "Nexi"; `Responsavel_Departamento == "Tendência"` → tag "Tendência". Pra ter tag "Automação" precisa: (1) Leo criar um campo Yes/No novo (ex. `veio_automacao`) na Data Type Solicitação no editor Bubble — Data API não cria campo novo sozinha, tentativa de mandar campo não cadastrado dá `400 Unrecognized field`; (2) eu adicionar a 3ª regra em `app.js`. Nenhuma das duas feitas ainda.
- **Custo Bubble (Workload Units):** 448 Solicitações criadas num disparo só (2026-08-20) — se o plano Bubble for por capacidade, vale confirmar com o Leo se isso é sustentável rodando toda checagem de dia 20 automaticamente.
