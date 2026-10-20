# Os seis segundos, agora com captura na matriz

[English](resilience-timeouts.en.md) · [Investigação anterior](pod-replacement.md)

Os dois CIs anteriores passaram nas checagens de recuperação, mas a matriz ainda registrou timeouts de cerca de seis segundos. As capturas daquela etapa cobriam outro teste, executado antes da matriz. Elas não permitiam explicar essas requisições.

Esta investigação coloca a captura na própria matriz, com os quatro perfis: sem/com drenagem, combinados com remoção abrupta/prazo de 30 segundos. Mantém quatro rodadas por perfil, dois trabalhadores por acesso e a mesma política de tentativas para GET. O timeout por operação de socket do acesso direto continua em seis segundos; o teste não foi acelerado para esconder a espera.

## O que passou a ser observado

A sonda em [resilience.py](../tests/resilience.py) guarda os horários de início e término, a porta de origem, o tempo de conexão e a fase que falhou: conexão, envio, cabeçalhos ou corpo. O conteúdo da requisição, a leitura da resposta e os limites continuam os mesmos. O ponto de criação do socket é instrumentado para reter sua porta mesmo se a conexão não terminar; os destinos do lab são endereços IPv4 numéricos.

O CI captura o tráfego da NodePort `30434` nos nós kind e no host do runner. Também captura TCP da porta `8000` dos IPs iniciais da API. Os arquivos têm cabeçalho Linux SLL2 e snapshot de 60 bytes, sem conteúdo HTTP. Endereços de Pods criados depois do início da tentativa não entram no filtro de backend; sua ausência nessa captura não prova descarte de pacotes.

A análise em [resilience_packets.py](../scripts/resilience_packets.py) cruza a tupla e o intervalo de cada requisição direta que falhou ou levou pelo menos 500 ms. Guarda ainda uma requisição saudável por tentativa. Busca SYN, SYN-ACK, dados enviados, ACKs, dados de resposta e RST. Usa sequência e horário do SYN para listar candidatos de tradução para os Pods iniciais. ACK não prova que o processo da aplicação aceitou ou leu a requisição.

Todas as amostras HTTP continuam no relatório. O cliente do acesso com tentativas conecta ao proxy local: sua porta de loopback não identifica cada tentativa interna na NodePort. Esses erros e contagens continuam visíveis, mas não são atribuídos a uma conexão específica sem evidência.

## Controles reais, separados da aplicação

[resilience_capture.py](../tests/resilience_capture.py) testa dois casos conhecidos com o mesmo cliente de seis segundos:

- Um servidor local recebe a requisição e não envia resposta. A espera deve terminar nos cabeçalhos, com conexão estabelecida e sem dados de resposta.
- Uma regra temporária descarta SYNs para uma porta comprovadamente sem serviço no nó kind. A espera deve terminar na conexão, com SYNs e sem SYN-ACK. A regra tem identificador próprio e é removida ao final.

Esses controles conferem a leitura da captura. Não contam como reprodução do timeout da API. A matriz continua usando as falhas reais do Chaos Mesh, com comparação dos dados originais, PVC e recursos dos outros labs.

## Resultados

A [primeira matriz local](../evidence/matrix-timeouts-local-resilience.json) passou nas 39 verificações ao longo de 16 remoções em 290,38 segundos. Essa primeira captura observou os nós kind; a observação no runner foi acrescentada para o CI. Nenhum timeout de seis segundos ocorreu nesta matriz. A [análise](../evidence/matrix-timeouts-local-resilience-timeouts.json) classificou 15 fluxos de requisições diretas com erro ou lentidão como `reset_seen` e uma requisição bem-sucedida de 4,086 segundos cuja tupla não teve correspondência dentro do intervalo registrado. Há um fluxo com a mesma porta fora desse intervalo; ele não foi deslocado nem atribuído por suposição. A lacuna continua sem explicação. As comparações das linhas, timestamps, identidade do PVC e recursos dos outros labs passaram.

Os [sete controles locais](../evidence/matrix-timeouts-local-resilience-controls.json) passaram. As duas esperas de seis segundos foram observadas e [classificadas pelos pacotes](../evidence/matrix-timeouts-local-resilience-controls-analysis.json): uma na conexão e outra aguardando cabeçalhos de resposta. Os timeouts desses controles não entram como reprodução da API.

## O que o CI reproduziu

O [run 39421860854](https://github.com/tommmdl/chaos-lab/actions/runs/39421860854), commit `2171884`, passou nas **146 verificações**. O [resumo](../evidence/matrix-timeouts-ci-39421860854-summary.json) guarda todas as etapas e o estado final do runner. A matriz completou 16 remoções em 181,96 segundos e reproduziu **cinco timeouts diretos**, todos ao **estabelecer a conexão TCP**, com duração de 6.001–6.006 ms. Em cada caso houve retransmissões de SYN sem SYN-ACK nem RST do servidor observado. Nenhuma requisição HTTP foi enviada nessas tentativas de conexão que falharam.

As [32 capturas da matriz](../evidence/matrix-timeouts-ci-39421860854-matrix-packet-archive.json) guardaram **432.610 quadros sem drops reportados**. A [análise offline](../evidence/matrix-timeouts-ci-39421860854-resilience-timeouts.json) reproduziu exatamente a análise online, sem tupla direta não resolvida neste CI. As análises da matriz, dos controles e dos RST da etapa anterior também foram reproduzidas a partir dos arquivos realocados. As checagens de recuperação, dados originais/timestamps, identidade do PVC e recursos dos outros labs passaram.

Os [casos individuais](../evidence/matrix-timeouts-ci-39421860854-cases.json) e a [linha do tempo resumida](../evidence/matrix-timeouts-ci-39421860854-timeline-summary.json) juntam a evidência de pacotes e Pods:

| Drenagem / prazo / rodada | Porta do cliente | Espera | Backend observado pela correspondência de SYN |
|---|---:|---:|---|
| não / 30 s / 2 | 35140 | 6,004 s | `10.244.0.40`, Pod selecionado `crgb9` |
| não / 30 s / 2 | 35148 | 6,006 s | `10.244.0.40`, Pod selecionado `crgb9` |
| não / 30 s / 3 | 42264 | 6,004 s | `10.244.0.41`, Pod selecionado `pqs4l` |
| não / 30 s / 3 | 42300 | 6,001 s | `10.244.0.41`, Pod selecionado `pqs4l` |
| sim / 0 s / 3 | 35158 | 6,003 s | Sem correspondência com backend inicial |

### Quatro conexões continuaram apontando para um Pod que saía

Na porta de cliente `35140`, o watch do Pod já havia informado o encerramento cerca de 417 ms antes do início da sonda. O watch de EndpointSlice removeu `10.244.0.40` cerca de 53 ms antes desse início. Mesmo assim, a correspondência de sequência/horário do SYN encontrou tráfego traduzido para o mesmo IP inicial do Pod, com retransmissões no nó por volta de 0, 1,06, 2,08, 3,11, 4,13 e 5,16 segundos. Não houve SYN-ACK. A sonda atingiu o timeout perto de seis segundos. Os outros três casos mostraram o mesmo padrão para o Pod selecionado na respectiva rodada. Os horários do watch são os de recebimento pelo cliente, não os da transição no kernel.

O padrão dos pacotes é compatível com uma janela de atualização do encaminhamento do Service, seguida da permanência de uma tradução NAT para o backend que saía. Isso é uma **inferência**, não uma causa medida dentro de kube-proxy/conntrack. O Kubernetes documenta que [as regras do Service podem ficar atrás das mudanças de EndpointSlice](https://kubernetes.io/docs/reference/networking/virtual-ips/#optimizing-iptables-mode-performance), e o Netfilter descreve [traduções NAT baseadas no acompanhamento das conexões](https://www.netfilter.org/projects/nftables/manpage.html). Esta matriz não guardou entradas de conntrack, snapshots de regras ou decisões de rota dessas tuplas; portanto, não identifica a regra ou rota exata responsável pela ausência de resposta TCP. Os pacotes de backend apareceram com traduções de origem do nó e do runner; a correspondência do IP não prova entrega ao processo da aplicação.

### Uma conexão já estava esperando antes da falha

A porta de cliente `35158` começou cerca de **917 ms antes** de o watch informar a exclusão do Pod da rodada atual. O horário registrado da aplicação do experimento também coloca o início da sonda **653 ms antes da chamada de aplicação**. Houve SYNs na NodePort tanto no nó quanto no runner, sem SYN-ACK, mas nenhum candidato de backend correspondeu ao filtro dos IPs iniciais. Não dá, portanto, para atribuir esse timeout ao Pod removido por aquela rodada. O destino após NAT e a razão da ausência de respostas continuam sem confirmação. A próxima lacuna de diagnóstico é acompanhar conntrack, regras e rotas junto dessas tuplas exatas.

A captura conta observações, não tentativas independentes: um SYN pode aparecer em mais de uma interface e nos dois pontos de coleta. Cada um dos quatro primeiros casos mostra seis envios de SYN no nó, não dezoito tentativas diferentes de conexão.

### O que ficou estabelecido e o que permanece aberto

Os seis segundos desses cinco casos são a **espera do cliente pela conexão**, não seis segundos atendendo `/healthz`. São diferentes dos resets já reproduzidos depois de completar o handshake. Os cinco timeouts atuais não provam retroativamente que todos os timeouts históricos sem instrumentação tiveram a mesma causa. O acesso com tentativas já usa conexão curta e repetição limitada de GET; esta investigação mantém a sonda direta como estava para deixar a falha visível. Ela não altera parâmetros de kube-proxy nem afirma que a drenagem elimina todas as falhas.

A comparação completa de HTTP ficou assim:

| Drenagem | Prazo | Falhas/requisições diretas | Falhas/requisições GET |
|---|---:|---:|---:|
| não | 0 s | 2/1330 | 0/1327 |
| não | 30 s | 21/759 | 5/1083 |
| sim | 0 s | 2/1070 | 0/1178 |
| sim | 30 s | 0/1505 | 0/1505 |

Cinco GETs com tentativas ainda terminaram em falha sem drenagem. Suas tentativas e erros de transporte continuam nas amostras. O perfil com drenagem e prazo de 30 segundos não teve falha HTTP final neste run, mas o CI anterior teve timeout nesse perfil. Os dois históricos continuam publicados. Os sete controles do CI também passaram; os dois timeouts provocados nos controles ficam separados dos cinco timeouts da API descritos acima.

## Repetir

Execute uma suíte de cada vez, com o ambiente do lab instalado:

```bash
python3 tests/resilience_capture.py
python3 tests/resilience.py --rounds 4 --capture-packets
python3 scripts/archive_packets.py --report evidence/resilience.json --output evidence/matrix-packet-archive.json
python3 scripts/analyze_resilience.py evidence/resilience.json --output evidence/resilience-timeouts.json
```

A captura exige `tcpdump` e permissão para entrar nos namespaces de rede dos nós. No WSL usado neste lab, `PACKET_PRIVILEGE=wsl` seleciona o acesso root; no CI, o runner usa `sudo`. Guarde cada execução com outro nome antes de repetir: os nomes canônicos em `evidence` são saídas da execução atual.

A análise offline verifica os hashes dos PCAPs, formato sem payload, contagem de quadros, drops e igualdade com a análise feita durante a execução. Se faltar evidência ou houver perda na coleta, isso aparece no relatório; não vira uma conclusão sobre o componente responsável.

Referências: [timeout do HTTPConnection](https://docs.python.org/3/library/http.client.html) e [encerramento de Pods e seus endpoints](https://kubernetes.io/docs/tutorials/services/pods-and-endpoint-termination-flow/).
