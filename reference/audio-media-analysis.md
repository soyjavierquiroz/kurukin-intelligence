# Media: evidencia y decisión 0.5.0

Referencia inspeccionada: myfavett-1.12.63/s.js, función minificada bestQuality y bloque de construcción de candidatos inmediatamente anterior. No se copia su implementación.

- Candidato base: playAddr, después downloadAddr, después PlayAddrStruct.UrlList[0]. Condiciona width y format mp4. Extrae bitrate, codecType y PlayAddrStruct.DataSize.
- Variantes: bitrateInfo[].PlayAddr.UrlList[0], PlayAddr.Width, PlayAddr.DataSize, Format, Bitrate, CodecType.
- La referencia selecciona calidad por anchura, preferencia HEVC configurable, bitrate y tamaño; para este PoC se prefiere compatibilidad y tamaño razonable.
- Nuestra implementación considera las listas de URLs, deduplica y limita a tres; prioriza bitrateInfo, playAddr/PlayAddrStruct y downloadAddr. No importa lógica de descarga/persistencia de myFaveTT.
- No hay respuesta real guardada del usuario en este workspace. Los fixtures originales solo tienen playAddr, downloadAddr y duration; los nuevos fixtures cubren las estructuras confirmadas por la referencia. Su presencia actual se determina al consumir cada item en MAIN, sin logs ni captura persistente.
- HasAudio/hasAudio y variantes minúsculas se admiten defensivamente; no se presentan como campos confirmados por la referencia. Si no se indica disponibilidad de audio, decodeAudioData es la prueba efectiva.
