# Investigando o timeout que restou no CI

[English](connection-timeout.en.md) · [Investigação anterior](connections.md) · [Início](../README.md)

A espera de drenagem ajudou na primeira comparação, mas uma requisição direta ainda falhou no GitHub. Voltei ao registro dessa conexão antes de mudar os tempos do Pod. Nesta etapa, mantive a API, o Service, as réplicas e o encerramento iguais. A mudança adotada está no acesso local de leitura: separar o prazo para abrir uma conexão do prazo para receber a resposta.

## O que o registro antigo permite concluir

Na rodada quatro com drenagem e prazo de 30 segundos, a requisição terminou em `TimeoutError` após 6.002,085 ms. O início estimado foi 704 ms depois da aplicação do experimento, já depois do watch receber o endpoint com `ready=false`. O log de SIGTERM só chegou cerca de 4,59 segundos depois de a requisição começar. A conexão começou enquanto a API ainda estava na espera de cinco segundos.

Isso enfraquece a hipótese de que faltou simplesmente mais tempo antes do SIGTERM. Não prova qual backend recebeu a conexão: o relatório antigo não guardava a fase TCP nem a porta de origem. Ele não distingue um SYN sem resposta de uma espera pelos cabeçalhos HTTP. Os horários dos watches são de recebimento, e o início da requisição foi calculado a partir da duração. O [recorte com os cálculos](../evidence/connection-window-original-timeout.json) aponta para o relatório original, que continua intacto.

Não atribuí o erro a DNS, PostgreSQL, saturação de conntrack ou a um bug específico do kernel. O cliente usa IP numérico e `/healthz`; faltam evidências para fechar as outras hipóteses. A [documentação de rede do Kubernetes](https://kubernetes.io/docs/reference/networking/virtual-ips/) descreve comportamentos de proxy e problemas conhecidos, mas a existência de um problema conhecido não identifica a causa deste timeout.

## O que mudou e por quê

GET agora permite **500 ms para estabelecer TCP**, conservando até três tentativas, as esperas de 50/100 ms, o prazo de leitura de até três segundos e o orçamento configurado de quatro segundos. Depois de conectar, o timeout de leitura também é limitado pelo tempo restante. POST e DELETE continuam com uma única tentativa e não recebem esse limite curto de conexão.

Cada falha interna registra `phase` (`connect`, `send`, `headers` ou `body`) e `elapsed_ms`, além do tipo do erro. Os cabeçalhos de tentativas e o log continuam visíveis. HTTP 503 e outras respostas HTTP não provocam nova tentativa. As sondas do Prometheus continuam sem passar pelo acesso com repetição.

Não aumentei o `preStop`, não removi a amostra com falha e não mudei o timeout de seis segundos do cliente direto do comparador novo. Se o acesso recuperar uma leitura, a conexão perdida continua no relatório. Esta mudança melhora a margem para tentar outra conexão; não corrige, por si, um pacote perdido na rede do cluster.

## Um teste de TCP que não depende da sorte da remoção

`tests/syn_budget.py` verifica primeiro uma porta sem serviço no nó, que recusa conexão. Depois instala uma regra temporária de descarte de SYN restrita àquela porta, IP do nó e IP de origem. A regra não atinge a API nem o banco e é removida no `finally`. São sockets e pacotes reais, sem simular a exceção em Python.

Na execução local, o acesso com conexão de três segundos esgotou cerca de **3.951 ms dentro de duas tentativas**. Com 500 ms, fez as três tentativas em cerca de **1.502 ms de espera de conexão**. Esses valores somam as tentativas e não incluem os intervalos de 50/100 ms. Nos dois casos, o destino continuou inacessível e o resultado foi 502. O teste demonstra classificação da fase, limite de espera e espaço para mais uma tentativa; não demonstra uma leitura recuperada nem reproduz a causa histórica. [Relatório completo](../evidence/syn-budget.json).

## Comparar sob remoções reais

`tests/connection_window.py --rounds 8` executa oito remoções com prazo e intercala quatro cortes abruptos. Cada rodada mantém três caminhos simultâneos: NodePort direto, acesso instrumentado com conexão de três segundos e acesso instrumentado com conexão de 500 ms. Cada caminho tem dois clientes com intervalo mínimo de 50 ms. Os dois acessos têm o mesmo código de medição e prazo de leitura; o caminho de três segundos representa o orçamento anterior, não uma reprodução byte a byte do código antigo.

O cliente direto registra início, fase, latência e porta de origem quando disponível. Watches acompanham Pods, EndpointSlices e sinais do Gunicorn. Um observador de conntrack no nó registra apenas metadados de conexões para o NodePort 30434; o relatório guarda os registros relacionados às portas de origem das requisições diretas que falharam. Não captura payload nem credenciais. Um registro `SYN_SENT` sozinho não prova que a conexão nunca avançou: a coleta pode perder eventos. Essa evidência deve ser lida junto da fase do socket.

Os três caminhos não enviam a mesma requisição ao mesmo backend. Timeouts e novas tentativas mudam o número de amostras. A escolha de réplica é aleatória. As contagens descrevem estas execuções e não formam uma medição de SLO.

## O que aconteceu na nova comparação local

As 12 remoções levaram 237,05 s e passaram em 17 verificações de recuperação e preservação. As oito remoções com prazo e os quatro cortes abruptos não produziram erro de conexão em nenhum dos três caminhos nesta execução.

| Remoção | Direto | Acesso TCP 3 s | Acesso TCP 500 ms |
| --- | --- | --- | --- |
| Prazo de 30 s, oito rodadas | 0 / 5.413 | 0 / 5.369 | 0 / 5.358 |
| Prazo zero, quatro rodadas | 0 / 2.505 | 0 / 2.497 | 0 / 2.492 |

Cada célula mostra falhas/requisições. Também não houve falha interna recuperada por novas tentativas. Como os dois acessos tiveram zero erros, esta amostra **não demonstra redução da taxa de falhas pela candidata**. Os cortes abruptos sem erro tampouco tornam prazo zero seguro. Os resultados anteriores com falhas continuam válidos como observações de outras execuções.

O [resumo](../evidence/connection-window-summary.json) e o [relatório com todas as amostras](../evidence/connection-window.json) ficam separados da comparação anterior. Não houve conexão direta com erro para correlacionar com conntrack nesta execução. A coleta concluiu com os observadores encerrados; dados originais, timestamps, PVC e workloads dos outros labs foram conferidos.

## A regressão também teve falhas

Repeti a matriz anterior de 16 remoções com o novo acesso, incluindo os perfis que retiram a espera de drenagem. Ela passou nas 23 asserções de recuperação, preservação e política de escrita em 283,24 s; isso não significa que todas as requisições passaram.

| Perfil local | Prazo | Falhas / direto | Falhas / GET com TCP curto | Falhas internas desse acesso |
| --- | --- | --- | --- | --- |
| Sem espera | 0 s | 43 / 1.557 | 4 / 1.966 | 42 |
| Sem espera | 30 s | 24 / 1.658 | 2 / 2.028 | 23 |
| Com espera | 0 s | 4 / 2.142 | 0 / 2.113 | 6 |
| Com espera | 30 s | 0 / 2.152 | 0 / 2.161 | 1 |

Mesmo com o acesso novo, seis leituras falharam nos perfis sem espera. Com espera e prazo, uma falha de transporte interna foi recuperada. Os conjuntos direto e com tentativas não são pareados. O [relatório desta regressão](../evidence/connection-timeout-resilience.json) mantém cada amostra e o teste real de POST com resposta perdida, que confirmou uma tentativa e uma gravação. O [relatório antigo](../evidence/resilience.json) não foi substituído.

## O CI trouxe erros diferentes do timeout antigo

O [workflow 39403569026](https://github.com/tommmdl/chaos-lab/actions/runs/39403569026) passou nas **90 verificações** em 9 min 32 s. O [registro da execução](../evidence/connection-timeout-ci-run.json) identifica o commit testado. A comparação nova levou 121,13 s:

| Remoção no CI | Direto | Acesso TCP 3 s | Acesso TCP 500 ms |
| --- | --- | --- | --- |
| Prazo de 30 s, oito rodadas | 0 / 3.346 | 0 / 3.346 | 0 / 3.343 |
| Prazo zero, quatro rodadas | 6 / 1.273 | 0 / 1.270 | 0 / 1.269 |

O timeout de seis segundos não se repetiu. Nos cortes abruptos houve **três resets na fase de cabeçalhos e três recusas na fase de conexão**, todas registradas no [recorte das seis falhas](../evidence/connection-timeout-ci-failures.json). Cada acesso com tentativas teve quatro falhas internas de transporte e nenhuma falha final. Os dois limites de conexão tiveram resultados finais iguais nesta amostra.

Para as três recusas, os tuples de conntrack apontam os IPs `10.244.0.46` e `10.244.0.51`, que pertenciam aos Pods em exclusão. As tentativas começaram cerca de 45–66 ms depois de o watch receber SIGTERM. Isso associa essas recusas à janela de encaminhamento para Pods que estavam encerrando no corte abrupto. Não associa o timeout antigo a esse mecanismo. Nos três resets, o cliente HTTP já havia fechado o socket quando a porta de origem foi consultada; ela ficou ausente, e não fiz correlação de tuple para esses resets. A fase do erro foi preservada.

A regressão de 16 remoções no mesmo runner também guardou suas falhas:

| Perfil no CI | Prazo | Falhas / direto | Falhas / GET com TCP curto | Falhas internas desse acesso |
| --- | --- | --- | --- | --- |
| Sem espera | 0 s | 8 / 1.261 | 0 / 1.257 | 7 |
| Sem espera | 30 s | 5 / 1.269 | 0 / 1.267 | 2 |
| Com espera | 0 s | 0 / 1.344 | 0 / 1.344 | 0 |
| Com espera | 30 s | 0 / 1.504 | 0 / 1.504 | 0 |

O descarte de SYN repetiu cerca de 3.951 ms de espera em duas tentativas contra 1.502 ms em três, sem recuperar o destino bloqueado. Os relatórios completos ficam separados: [comparação nova](../evidence/connection-timeout-ci-connection-window.json), [resumo](../evidence/connection-timeout-ci-summary.json), [matriz de regressão](../evidence/connection-timeout-ci-resilience.json), [SYN](../evidence/connection-timeout-ci-syn-budget.json), [protocolo HTTP](../evidence/connection-timeout-ci-access-policy.json) e [alertas e recuperação](../evidence/connection-timeout-ci-verification.json).

## O que foi adotado e o que continua aberto

Passaram **90 verificações locais e 90 no CI**: 43 dos experimentos e alertas, 23 da matriz anterior, quatro de protocolo, três do descarte de SYN e 17 da comparação nova. A [regressão local de alertas](../evidence/connection-timeout-verification.json) levou 198,03 s e terminou com duas APIs prontas, PostgreSQL pronto, dez notas, o mesmo PVC e nenhum alerta ativo. Os testes acrescentaram três notas; as sete que já existiam permaneceram iguais.

Adotei o limite TCP curto para GET e os logs por fase. O ganho demonstrado é limitar a espera de conexão e permitir três tentativas quando SYNs são descartados. **Não foi demonstrada uma redução adicional da taxa de erros na remoção com prazo**, porque os dois acessos passaram sem erros nas amostras novas. A causa exata do timeout histórico continua aberta. Os erros de corte abrupto ficaram mais bem identificados, e a espera de drenagem continua sendo parte da configuração; novas tentativas não tornam escritas seguras durante uma interrupção.

O [resumo de validação](../evidence/connection-timeout-validation.json) lista os cinco relatórios de cada ambiente. O [estado final local](../evidence/connection-timeout-final-state.json) confirma o limite ativo, nenhum experimento e nenhum observador restante. Os outros labs mantiveram oito, sete e 14 notas.

## Reproduzir

```bash
python3 tests/access_policy.py
python3 tests/syn_budget.py
python3 tests/connection_window.py --rounds 8
python3 tests/resilience.py --rounds 4
python3 tests/verify.py
```

O teste de SYN usa Docker e o `iptables` disponível no nó kind. O observador usa o `conntrack` desse nó; seu processo é encerrado explicitamente, antes de fechar o cliente Docker. A primeira coleta foi interrompida após identificar observadores que sobreviviam ao cliente. Duas rodadas concluídas ficaram no [relatório parcial](../evidence/connection-window-initial-capture.json), marcado como incompleto e sem resultado aprovado. Os observadores iniciais foram encerrados e a comparação completa foi refeita com a limpeza corrigida.

Para ativar a mudança no navegador, reinicie `python3 scripts/access.py`. Para retornar somente ao limite anterior, mude `get_connect_timeout_seconds` para 3 e reinicie esse processo; não é necessário trocar a imagem nem tocar nos dados. Para remover as tentativas de GET, use `get_max_attempts: 1`. Os relatórios e o acesso direto continuam disponíveis nas duas opções.

500 ms é uma escolha para a rede local deste lab. Uma rede lenta pode precisar de mais tempo para conectar, e uma tentativa abandonada acrescenta carga. O teste não garante que um serviço remoto com latência maior se beneficie desse valor. Escritas com resultado incerto continuam exigindo conferência antes de qualquer repetição.
