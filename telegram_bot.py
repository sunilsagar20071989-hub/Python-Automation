import telebot

TOKEN = "8560792327:AAErjHTU4LlKxlueD4c-EXxS2KcqVwBrDN8"  # Yahan BotFather wala Token daalein
bot = telebot.TeleBot(TOKEN)

@bot.message_handler(func=lambda message: True)
def get_chat_id(message):
    print(f"Aapki Chat ID ye hai: {message.chat.id}")
    bot.reply_to(message, f"Aapki Chat ID hai: {message.chat.id}")

print("Bot sun raha hai... ab Telegram par apne bot ko 'Hi' bhejein.")
bot.polling()
run: |
