# O que os experimentos mostraram

[English](validation.en.md) · [Procedimento](runbook.md)

Em 19/10/2026, passaram **43 verificações locais em 183,87 segundos**. Foram cinco experimentos reais do Chaos Mesh, com três alertas acompanhados do Prometheus até o webhook local. Para cada um desses alertas, firing e resolved tiveram o mesmo fingerprint. As seis notificações conferidas sobreviveram à substituição do Pod do receptor.

| Experimento | Resultado local |
| --- | --- |
| Uma réplica da API removida abruptamente | 14 de 16 amostras responderam HTTP 200. Duas tiveram falha de conexão. O Pod foi substituído e não houve alerta de indisponibilidade sustentada. |
| Atraso de 100 ms por pacote entre API e banco | A sonda de readiness levou cerca de 837 ms e continuou bem-sucedida. O alerta de lentidão disparou e foi resolvido depois da remoção do experimento. |
| Partição API → banco por 60 s | Liveness permaneceu saudável e readiness falhou. O alerta de dependência chegou ao receptor. O recurso continuava presente quando `AllRecovered` confirmou a recuperação por duração. O ciclo até conferir recuperação e notificação levou 64,73 s. |
| Falha nas duas réplicas da API | As duas sondas falharam e o alerta crítico chegou ao receptor. A remoção normal restaurou as imagens originais da API e entregou resolved. |
| Pod do PostgreSQL removido abruptamente | O banco voltou com um UID de Pod novo e o mesmo UID de PVC. Linhas, IDs e timestamps permaneceram iguais; uma nova nota foi gravada pela API. |

O estado final tem duas réplicas prontas da API, um PostgreSQL, duas notas, as duas sondas com valor 1 e nenhum alerta ativo. Nenhum experimento ficou pendente e `cleanup_errors` ficou vazio. As configurações de workloads, imagens e UIDs de PVCs dos outros labs foram comparadas antes e depois e permaneceram iguais.

## As duas falhas de conexão fazem parte do resultado

Passar nas verificações não significou zero erro durante a remoção de uma réplica. No relatório, o valor `0` em uma amostra indica falha no cliente sem resposta HTTP; não é um status retornado pelo servidor. As outras amostras responderam 200. A coleta não identifica sozinha a causa exata de cada falha.

A hipótese de recuperação das réplicas foi confirmada. A de ausência completa de erros transitórios não foi demonstrada. O alerta exige falha sustentada por 9 segundos, e sondas periódicas não enxergam necessariamente toda falha de uma requisição de usuário. Drenagem de conexões, encerramento gracioso, métricas de requisições e um SLO de erros são caminhos para uma próxima experiência. O teste mantém esses erros visíveis.

## Decisões que surgiram na preparação

O kernel do WSL tinha `sch_netem` disponível como módulo, mas ele ainda não estava carregado. Foi carregado pelo root do WSL antes dos testes de atraso. O teste completo confere esse requisito antes de injetar falhas.

O modo de namespace do chart 2.8.4 deixou faltando leituras de namespace e do recurso global `RemoteCluster`, que o controlador tentava observar. Foi acrescentado um papel apenas de leitura para descoberta. O teste confirma que a conta do controlador pode remover Pods em `chaos-demo` e não pode fazer isso em `notes-lab`. Isso não elimina os privilégios do daemon sobre o runtime do nó.

Na primeira configuração do Prometheus, o timeout de coleta era maior que o intervalo, e ele recusou iniciar. O intervalo passou a 5 segundos, com timeout de 4 segundos. As configurações montadas foram conferidas pelos validadores do Prometheus e do Alertmanager antes dos experimentos.

O banco ficou com um Service headless para que o destino da conexão correspondesse aos IPs de Pod selecionados pelo NetworkChaos. Os testes usam uma cópia própria da aplicação; não houve necessidade de pausar Argo CD ou alterar as regras compartilhadas dos labs anteriores.

## Ler as evidências

[verification.json](../evidence/verification.json) contém as verificações, as amostras de HTTP, a latência observada, notificações conferidas, imagens e o estado final. Os outros JSONs guardam os objetos de experimento com seus alvos e registros de injeção e recuperação. `03-database-partition-injected.json` e `03-database-partition-recovered.json` permitem comparar o mesmo UID antes e depois de expirar a duração.

O tempo `seconds_until_recovery_checked` termina depois de conferir a recuperação, a mensagem resolved e os dados. Não é uma medição contínua de indisponibilidade. O tempo da execução completa inclui esperas de sondas, regras, entrega e recuperação, mas não inclui a instalação inicial.

As notas reais usadas como referência e a senha ficam em `.local`. O relatório público guarda contagens, verificações e hash das linhas, sem exportar a base de dados ou credenciais. O GitHub Actions cria um cluster novo e repete o mesmo roteiro; seus resultados são guardados em artefato.

Os resultados cobrem este ambiente e estes parâmetros. Não houve perda da máquina, falha de armazenamento físico, falha simultânea do controlador de caos e da aplicação ou entrega a uma ferramenta externa de plantão. A amostragem de HTTP também não estabelece um percentual de disponibilidade para produção.
