require("dotenv").config();

const OpenAI = require("openai");

const client = new OpenAI({
  apiKey: process.env.OPENAI_API_KEY,
});

async function generarSistema() {

  const response = await client.responses.create({
    model: "gpt-5-mini",
    input: "Genera un sistema RF60 para hotel en zona húmeda Caribe",
  });

  console.log(response.output_text);
}

generarSistema();

