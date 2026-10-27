# Akıllı Kart Platformu (Dinamik QR Kart Etkinleştirme)

Basılan her kartta tek bir QR kod bulunur ve şu adrese işaret eder:

```
https://ALAN-ADINIZ/c/<kart_kodu>
```

| Kartın durumu | Okutulunca ne olur |
|---|---|
| Yeni kart (etkinleştirilmedi) | Hedef bağlantının girildiği etkinleştirme sayfası açılır (Google yorum bağlantısı, Instagram vb.) |
| Aktif kart | Kayıtlı bağlantıya anında yönlendirilir (302) |
| Duraklatılmış kart | «Kart duraklatıldı» mesajı gösterilir |
| Kayıtsız kod | 404 |

Yönetim paneli: `/admin`

## Hızlı başlangıç (Docker, önerilen)

```bash
cp .env.example .env        # BASE_URL, ADMIN_PASSWORD ve SECRET_KEY değerlerini doldurun
docker compose up -d --build
```

Uygulama `http://localhost:8000` adresinde açılır. Veritabanı `cards-data` kalıcı biriminde tutulur, yeniden başlatmalarda silinmez.
Canlıda uygulamanın önüne HTTPS sağlayan bir ters vekil koyun (Caddy, nginx veya barındırma platformunun kendi HTTPS'i).

## Docker olmadan

```bash
pip install -r requirements.txt
ADMIN_PASSWORD='guclu-bir-sifre' python app.py          # geliştirme: http://localhost:5000
```

Üretimde:

```bash
export BASE_URL=https://kartlar.ornek.com
export ADMIN_PASSWORD=...
export SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
export TRUST_PROXY=1                    # nginx veya platform vekili arkasındaysanız
export DATABASE=/var/data/cards.db      # kalıcı bir disk yolu
gunicorn -w 2 -b 0.0.0.0:8000 app:app
```

`Procfile` dahildir; Railway, Render, Heroku gibi platformlarda doğrudan çalışır. SQLite kullanıldığı için platformda **kalıcı disk (volume)** bağlayıp `DATABASE` değişkenini o diske yönlendirin, aksi halde her dağıtımda kartlar silinir.
Sağlık kontrolü adresi: `/healthz`.

> `BASE_URL` içindeki alan adı kartlara kalıcı olarak basılacaktır. Sahip olacağınız ve uzun süre kullanacağınız bir alan adı seçin.

## Ortam değişkenleri

| Değişken | Açıklama |
|---|---|
| `BASE_URL` | QR bağlantılarının tabanı (https ile) |
| `ADMIN_PASSWORD` | Yönetici şifresi. Boşsa rastgele üretilip `.admin_password` dosyasına yazılır |
| `SECRET_KEY` | Oturum imzası. Boşsa `.secret_key` dosyasına yazılır |
| `DATABASE` | SQLite dosya yolu (varsayılan: `cards.db`) |
| `TRUST_PROXY` | `1` ise `X-Forwarded-*` başlıklarına güvenilir |
| `DISPLAY_TZ` | Panelde gösterilen saat dilimi (varsayılan: `Europe/Istanbul`) |
| `PORT` | Dinlenecek port |

## İş akışı

1. `/admin` içinden bir kart partisi oluşturun (ör. 100 adet). Her karta 8 karakterlik rastgele bir kod verilir.
2. «CSV dışa aktar» ile listeyi indirin; `qr_link` sütunu her kartın QR koduna yazılacak bağlantıdır. İsterseniz kartın sayfasından QR görselini SVG olarak da indirebilirsiniz.
3. Kartları basıp teslim edin. İşletme sahibi kartı ilk kez okutur, bağlantısını yapıştırır ve kart etkinleşir.
4. Sonradan panelden bağlantıyı değiştirebilir, kartı duraklatabilir, «etkinleştirilmedi» durumuna döndürebilir veya silebilir; okutma sayısını ve son okutma zamanını izleyebilirsiniz.

## Güvenlik notları

- **Etkinleştirilmemiş bir kartı ilk okutan kişi, kartı kendi bağlantısına bağlayabilir.** Kartları teslimden önce kendiniz etkinleştirin veya doğrudan işletme sahibine verin. Daha güçlü koruma için kartın arkasına basılan kısa bir «etkinleştirme kodu» eklenebilir.
- Yalnızca http/https bağlantıları kabul edilir (`javascript:` vb. reddedilir) ve bağlantı platformun kendi alan adına işaret edemez.
- Tüm formlarda CSRF koruması vardır; yanlış şifreyle 10 dakikada 5 denemeden sonra giriş geçici olarak engellenir.
- Yönlendirme 302 ve `no-store` ile yapılır; panelde yaptığınız değişiklik hemen geçerli olur.

## Test

```bash
python test_flow.py     # tüm akışı kapsayan testler
```
