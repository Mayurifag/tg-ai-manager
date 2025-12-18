# Messages queue

## To show on web interface

We have to queue all messages

## To autoread or skip or delete or else

Before message goes to AI we can:

- Skip if author is user account itself (this actually covers case of Saved Messages chat)
- Skip if message has specific link or regexp on words
- Skip if author is specific user. @lolsBotCatcherBot i.e. - Should I just skip all bots messages? Yes for now I guess.
- Skip if event is pinning or editing or deletion, we just have to parse msgs
- Photo changed, game scores, forum topic actions

## Not to forget

- Index (composite?) on chat_type (forum/channel/user) and its id for messages
  to see inclusion
- What if we have completely new chat? We have to follow only whole logics.
  Maybe we can actually implement new logics which will autodelete and autoban
  scam messages. Not sure of this. Maybe waywayway later on future.
