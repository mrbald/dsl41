# Block cards

A block card is a short reader's page about one runner building block.
A reviewer reads [the architecture overview](../architecture.md) first,
then one card at a time.
Cards hold no rules. Each invariant on a card links the decision or the
contract section that states it. A card never restates a frozen table; it
links it.

## The card shape

Every card has these sections, in this order:

1. **Purpose.** What the block is for, in two or three sentences.
2. **Fate.** Whether the block's code and contract would carry over if the
   storage under the runner changed, and which storage capabilities it
   needs. The capabilities are the rows of
   [concurrency-model §1](../concurrency-model.md#1-storage--frozen)'s
   storage contract: epoch-conditional append, monotone epoch allocation,
   decision lookup by `request_id`, atomic multi-record commit, and
   linearizable read of the leader record. A block that needs none says
   "none".
3. **Interface.** What the block takes and gives, as module functions,
   records, verbs or files.
4. **States.** A Mermaid state diagram that illustrates the block. It is a
   picture, not an inventory: it carries no row ids and makes no coverage
   claim.
5. **Invariants.** Each one links its decision-log entry or contract
   section.
6. **Failure and recovery.** What can go wrong and what brings the block
   back, with links.
7. **Owning modules.** The modules under `src/dsl41/` that implement it.
8. **Tests.** The tests that exercise it, by name, so the architecture
   gate checks that they exist.
9. **Open findings.** A link to the block's row in
   [the risk map](../risk-map.md).

A card that finds a gap, or a contradiction between the code and a
contract, lists it under **Gaps found** at the end. A card never settles
one.

## Cards

The list follows the review order.
