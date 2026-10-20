# De onde vêm os RST ao remover uma réplica

[English](rst-origin.en.md) · [Captura anterior](connection-timeout.md) · [Início](../README.md)

A captura anterior mostrava resets com o endereço da NodePort. Isso identificava o pacote que chegou ao cliente, mas não o emissor antes da tradução de rede. Nesta etapa, cruzei o mesmo RST dos dois lados da tradução com o evento do kernel que o enviou. Mantive a aplicação, as duas réplicas, a espera de drenagem e os limites das tentativas de GET.

## Como acompanhei cada reset

O teste continua comparando o acesso direto, o acesso com conexão TCP de três segundos e o acesso com conexão de 500 ms. `--attribute-resets` acrescenta estas observações antes de cada remoção real:

1. Registra o nome, UID, IP, nó e namespace de rede dos dois Pods da API. A rota para cada IP identifica o veth e seu índice no nó. Os PIDs do Gunicorn são associados ao mesmo namespace de rede, sem guardar variáveis de ambiente do container.
2. Captura o TCP da NodePort `30434` e **somente os RST** da porta `8000` dos IPs iniciais da API. Isso evita incluir todo o tráfego de sondas na captura, problema que já causou drops na etapa anterior. Mantém `LINUX_SLL2` e snapshot de 60 bytes; nenhum conteúdo HTTP cabe na captura.
3. Cria uma instância própria de tracefs, com `tcp:tcp_send_reset`, relógio monotônico e stack do kernel. O buffer recebe eventos de reset do kernel compartilhado; o relatório retém apenas os que envolvem um IP inicial da API e a porta `8000`. Não publica eventos de outros serviços nem o buffer bruto do host.
4. Encerra os coletores, remove a instância de tracefs e o experimento, e só então valida a evidência. Compara flags, sequência e ACK dos RST antes/depois da NAT, dentro de 20 ms. Exige o tuple do backend no evento do kernel e verifica se o pacote entrou no nó pelo veth previamente associado ao Pod (`packet_type=0`).

A leitura de metadados não mantém o namespace de rede do Pod aberto durante sua remoção. Os PCAPs são feitos no namespace dos nós. Ainda existe custo de observação e efeito de agendamento; os resultados não medem ganho de disponibilidade nem formam um SLO.

O relatório preserva eventos sem correspondência e usa uma classificação explícita de não resolvido quando falta observação. O nome da tarefa numa interrupção de rede, sozinho, não identifica o responsável: por exemplo, um evento pode aparecer no contexto de kubelet enquanto a pilha TCP do Pod responde a uma sonda.

## Um controle real para conferir o rastreador

Antes de remover Pods, o teste abre um servidor TCP numa porta local aleatória, envia bytes e fecha o socket aceito sem ler esses bytes. O cliente recebe `ConnectionResetError`; a captura do kernel mostra `tcp_send_active_reset`, `tcp_close` e o motivo de reset quando o kernel o disponibiliza. Esse controle comprova a coleta e a interpretação do mecanismo, mas não substitui a correlação com uma conexão real da API.

O [código TCP do Linux](https://kernel.googlesource.com/pub/scm/linux/kernel/git/torvalds/linux/+/ca13ab654064fee86d6e7c9e87d0af7789561509/net/ipv4/tcp.c) associa o fechamento com dados ainda não lidos ao reset ativo e ao motivo `TCP_ABORT_ON_CLOSE`. Um reset por `NO_SOCKET` percorre outro caminho, sem uma conexão aceita para encerrar. A [definição do tracepoint](https://github.com/torvalds/linux/blob/v6.11/include/trace/events/tcp.h) registra o tuple e, em kernels com esse campo, o motivo. A [documentação de ftrace](https://docs.kernel.org/trace/ftrace.html#instances) explica os buffers por instância e os stacks por evento. O relatório salva a versão do kernel e o formato real do evento; não assume que todos os kernels possuem os mesmos campos.

## Resultado local

A rodada local passou em **24 verificações, em 145,06 segundos**, com quatro remoções graciosas e dois controles abruptos. Os três caminhos terminaram sem falhas de requisição. O direto teve 0/2.770 falhas nas remoções graciosas e 0/1.328 nos cortes abruptos.

O controle positivo registrou `TCP_ABORT_ON_CLOSE` e o caminho de fechamento TCP. Houve eventos de reset em outras conexões dos Pods durante encerramentos graciosos, mas nenhum reset nas requisições diretas medidas. Eles ficaram no relatório como eventos do kernel sem associação a falha direta. Portanto, **esta amostra local não atribui os RST anteriores do CI**. Nenhuma rodada teve drops de PCAP ou overrun reportado no tracefs. Dados originais, timestamps e PVC do PostgreSQL permaneceram iguais; os workloads dos outros labs também foram comparados.

[Relatório com todas as amostras e eventos](../evidence/rst-origin-local.json) · [índice das capturas locais](../evidence/rst-origin-local-archive.json)

## Resultado no CI

O [run 39411512758](https://github.com/tommmdl/chaos-lab/actions/runs/39411512758), no kernel `6.17.0-1022-azure`, passou nas **115 verificações** da suíte. A matriz com atribuição passou em 42 asserções, em 141,57 s. Todos os PCAPs tiveram seus hashes conferidos: 12 arquivos, 166.143 frames, zero drops de captura, zero payload HTTP e nenhum overrun reportado no tracefs.

| Remoção no comparador | Falhas / direto | Falhas / TCP 3 s | Falhas / TCP 500 ms | Falhas internas recuperadas, 3 s / 500 ms |
| --- | --- | --- | --- | --- |
| 30 s, oito rodadas | 0 / 3.488 | 0 / 3.486 | 0 / 3.485 | 0 / 0 |
| Zero, quatro controles | 0 / 1.462 | 0 / 1.460 | 0 / 1.461 | 2 / 1 |

Nenhuma requisição direta falhou nesta rodada. Isso não apagou os resets: os acessos com novas tentativas registraram três erros internos e a captura guardou três RST enviados pelo servidor. Por isso, acrescentei uma análise de **todos** os RST observados na NodePort, além da análise das amostras diretas com erro. Ela também pode ser executada depois do teste, lendo os PCAPs comprimidos.

### A origem dos três pacotes desta execução

Os três saíram do Pod `api-5459756fdf-6fhmq`, IP `10.244.0.25`, namespace de rede `4026532975`. A captura os recebeu em `veth178953dc`, índice 26, com `packet_type=0`: entraram no nó vindos do Pod. Flags, sequence e ACK coincidem com os RST que saíram com o endereço da NodePort. O evento do kernel tem o tuple do backend e o mesmo instante dentro da janela de correlação.

| Porta do cliente na NodePort | Motivo no kernel | Caminho observado |
| --- | --- | --- |
| 35592 | `TCP_STATE` | `tcp_close → __tcp_close → inet_csk_listen_stop → inet_child_forget → tcp_disconnect → tcp_send_active_reset` |
| 35654 | `NO_SOCKET` | `tcp_v4_rcv → tcp_v4_send_reset` |
| 35664 | `NO_SOCKET` | `tcp_v4_rcv → tcp_v4_send_reset` |

No primeiro caso, o evento veio do PID de host **26201**, `gunicorn`, PID **16 no Pod**, associado ao mesmo namespace na fotografia anterior à falha. O fluxo completou o handshake e teve seus dados confirmados antes do RST. O stack mostra o fechamento do listener, que descarta conexões ainda na fila de aceitação; esse caminho é descrito em [`inet_csk_listen_stop`](https://github.com/torvalds/linux/blob/v6.17/net/ipv4/inet_connection_sock.c). O motivo real foi **`TCP_STATE`**, e não o `TCP_ABORT_ON_CLOSE` do controle de dados não lidos. Não atribuí esse reset a uma resposta da rota Flask.

Nos dois casos seguintes, o SYN recebeu RST sem SYN-ACK. A pilha TCP do mesmo Pod registrou `NO_SOCKET` e `tcp_v4_send_reset`, coerente com uma tentativa chegando depois do fechamento do listener. A tarefa aparece como `python3` porque o processamento ocorreu em contexto de rede durante a conexão do cliente; não é um PID Gunicorn e não foi tratado como emissor da API. O [código TCP IPv4](https://github.com/torvalds/linux/blob/v6.17/net/ipv4/tcp_ipv4.c) mostra o caminho de reset sem socket.

**A origem desses três RST é a pilha TCP Linux do Pod da API.** No primeiro, o fechamento do listener no contexto do Gunicorn provocou o reset da conexão pendente. Nos outros, o kernel respondeu a SYNs sem socket válido. A NodePort traduziu o endereço desses pacotes; ela não é a origem identificada para eles. Não é uma atribuição retroativa dos sete resets/recusas anteriores, nem de todo reset possível num cluster.

Os logs do acesso não guardam a porta de origem de cada tentativa, então não atribuí cada pacote a uma das duas modalidades de GET. A contagem agregada de três erros recuperados é compatível com os três pacotes, mas o vínculo individual é entre **PCAP, veth, tuple e evento de kernel**, não com um ID da requisição do proxy. Nenhuma observação de RST dessa captura ficou sem atribuição.

[Análise de todos os RST e seus stacks](../evidence/rst-origin-ci-flows.json) · [relatório completo](../evidence/rst-origin-ci.json) · [resumo e commit testado](../evidence/rst-origin-ci-summary.json) · [índice SHA-256](../evidence/rst-origin-ci-archive.json) · [PCAPs](../evidence/rst-origin/ci-39411512758-packets)

### A regressão separada também teve falhas

A matriz de resiliência posterior passou em 23 verificações de recuperação e política, mas teve falhas de requisição. Esses cenários rodam sem o rastreamento desta etapa; não atribuí seus pacotes usando a captura de outro intervalo.

| Perfil | Prazo | Falhas / direto | Falhas / GET com tentativas | Falhas internas recuperadas |
| --- | --- | --- | --- | --- |
| Sem espera | 0 s | 16 / 825 | 0 / 1.430 | 21 |
| Sem espera | 30 s | 11 / 1.020 | 0 / 1.469 | 15 |
| Com espera | 0 s | 5 / 1.106 | 0 / 1.180 | 5 |
| Com espera | 30 s | 0 / 1.537 | 0 / 1.538 | 0 |

Também ficaram preservados os relatórios de [resiliência](../evidence/rst-origin-ci-resilience.json), [alertas e recuperação](../evidence/rst-origin-ci-verification.json), [política HTTP](../evidence/rst-origin-ci-access-policy.json) e [descarte real de SYN](../evidence/rst-origin-ci-syn-budget.json). CI verde significa que as asserções passaram, não que toda conexão resistiu às falhas injetadas.

## Reproduzir e conferir

O runner usa Linux, Docker, `tcpdump` e tracefs com `tcp:tcp_send_reset` e stacks habilitados no kernel. A instalação do CI confere esse suporte antes de criar o cluster. Localmente, o cluster deve estar pronto e sem experimentos ativos:

```bash
python3 tests/connection_window.py --rounds 8 --attribute-resets
python3 scripts/archive_packets.py
python3 scripts/analyze_resets.py evidence/connection-window.json --output evidence/reset-origins.json
```

Neste WSL, use `PACKET_PRIVILEGE=wsl` antes do comando do teste. Num Linux comum, o coletor usa `sudo -n`. Não rode dois testes de mutação ao mesmo tempo. O teste recusa substituir experimentos existentes e usa o lock do lab.

Para repetir apenas a análise dos arquivos já publicados, use `python3 scripts/analyze_resets.py evidence/rst-origin-ci.json --output .local/rst-origin-recheck.json`; não é preciso acessar o cluster. A ferramenta confere o SHA-256 original e não considera a ausência de RST como prova de atribuição: nesse caso, registra `all_observed_resets_attributed=null`.

A integridade de PCAP e os contadores de perda por CPU do tracefs são verificados depois de guardar os resultados da rodada. Ausência de tracepoint, erro do controle positivo, perda ou falha de limpeza deixam o teste vermelho. O workflow tenta enviar os diagnósticos mesmo quando uma etapa falha; as capturas originais ficam como artefato por 30 dias. As cópias comprimidas e seus hashes são preservadas no repositório.

Também corrigi referências de arquivos nos relatórios de captura anteriores: algumas apontavam para o nome sem `.gz` ou para um prefixo local inexistente no Git. Conferi o SHA-256 dos bytes originais e das cópias comprimidas; pacotes, amostras e contagens antigas não foram alterados. O arquivador agora grava os caminhos comprimidos no relatório e consegue conferir um arquivo já arquivado numa nova execução.

## O que esta investigação pode concluir

Um RST observado no veth do Pod, com o mesmo sequence/ACK do RST recebido pelo cliente e o evento correspondente de envio TCP do kernel, permite atribuir **aquele pacote** à pilha TCP do Pod. Um PID associado ao namespace do mesmo Pod e um stack de fechamento podem identificar também o contexto de processo. A falta dessa combinação fica como não resolvida; não é evidência de que kube-proxy, iptables ou o cliente gerou o reset.

O timeout histórico de seis segundos aconteceu numa remoção graciosa sem captura na ocasião. Mesmo que a origem dos resets atuais seja identificada, isso não determina retroativamente a causa daquele timeout.
