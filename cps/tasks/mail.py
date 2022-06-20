from __future__ import division, print_function, unicode_literals
import sys
import os
import smtplib
import threading
import socket
import mimetypes
import base64

try:
    from StringIO import StringIO
    from email.MIMEBase import MIMEBase
    from email.MIMEMultipart import MIMEMultipart
    from email.MIMEText import MIMEText
except ImportError:
    from io import StringIO
    from email.mime.base import MIMEBase
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText



from email import encoders
from email.utils import formatdate, make_msgid
from email.generator import Generator

from cps.services.worker import CalibreTask
from cps.services import gmail
from cps import logger, config

from cps import gdriveutils

log = logger.create()

CHUNKSIZE = 8192


# Class for sending email with ability to get current progress
class EmailBase:

    transferSize = 0
    progress = 0

    def data(self, msg):
        self.transferSize = len(msg)
        (code, resp) = smtplib.SMTP.data(self, msg)
        self.progress = 0
        return (code, resp)

    def send(self, strg):
        """Send `strg' to the server."""
        log.debug_no_auth(f'send: {strg[:300]}')
        if not hasattr(self, 'sock') or not self.sock:
            raise smtplib.SMTPServerDisconnected('please run connect() first')
        try:
            if self.transferSize:
                lock=threading.Lock()
                lock.acquire()
                self.transferSize = len(strg)
                lock.release()
                for i in range(0, self.transferSize, CHUNKSIZE):
                    if isinstance(strg, bytes):
                        self.sock.send((strg[i:i + CHUNKSIZE]))
                    else:
                        self.sock.send((strg[i:i + CHUNKSIZE]).encode('utf-8'))
                    lock.acquire()
                    self.progress = i
                    lock.release()
            else:
                self.sock.sendall(strg.encode('utf-8'))
        except socket.error:
            self.close()
            raise smtplib.SMTPServerDisconnected('Server not connected')

    @classmethod
    def _print_debug(cls, *args):
        log.debug(args)

    def getTransferStatus(self):
        if self.transferSize:
            lock2 = threading.Lock()
            lock2.acquire()
            value = int((float(self.progress) / float(self.transferSize))*100)
            lock2.release()
            return value / 100
        else:
            return 1


# Class for sending email with ability to get current progress, derived from emailbase class
class Email(EmailBase, smtplib.SMTP):

    def __init__(self, *args, **kwargs):
        smtplib.SMTP.__init__(self, *args, **kwargs)


# Class for sending ssl encrypted email with ability to get current progress, , derived from emailbase class
class EmailSSL(EmailBase, smtplib.SMTP_SSL):

    def __init__(self, *args, **kwargs):
        smtplib.SMTP_SSL.__init__(self, *args, **kwargs)


class TaskEmail(CalibreTask):
    def __init__(self, subject, filepath, attachment, settings, recipient, taskMessage, text, internal=False):
        super(TaskEmail, self).__init__(taskMessage)
        self.subject = subject
        self.attachment = attachment
        self.settings = settings
        self.filepath = filepath
        self.recipent = recipient
        self.text = text
        self.asyncSMTP = None
        self.results = {}

    def prepare_message(self):
        message = MIMEMultipart()
        message['to'] = self.recipent
        message['from'] = self.settings["mail_from"]
        message['subject'] = self.subject
        message['Message-Id'] = make_msgid('calibre-web')
        message['Date'] = formatdate(localtime=True)
        text = self.text
        msg = MIMEText(text.encode('UTF-8'), 'plain', 'UTF-8')
        message.attach(msg)
        if self.attachment:
            if result := self._get_attachment(self.filepath, self.attachment):
                message.attach(result)
            else:
                self._handleError(u"Attachment not found")
                return
        return message

    def run(self, worker_thread):
        try:
            # create MIME message
            msg = self.prepare_message()
            if self.settings['mail_server_type'] == 0:
                self.send_standard_email(msg)
            else:
                self.send_gmail_email(msg)
        except MemoryError as e:
            log.debug_or_exception(e)
            self._handleError(f'MemoryError sending email: {str(e)}')
        except (smtplib.SMTPException, smtplib.SMTPAuthenticationError) as e:
            log.debug_or_exception(e)
            if hasattr(e, "smtp_error"):
                text = e.smtp_error.decode('utf-8').replace("\n", '. ')
            elif hasattr(e, "message"):
                text = e.message
            elif hasattr(e, "args"):
                text = '\n'.join(e.args)
            else:
                text = ''
            self._handleError(f'Smtplib Error sending email: {text}')
        except socket.error as e:
            log.debug_or_exception(e)
            self._handleError(f'Socket Error sending email: {e.strerror}')
        except Exception as ex:
            log.debug_or_exception(ex)
            self._handleError(f'Error sending email: {ex}')


    def send_standard_email(self, msg):
        use_ssl = int(self.settings.get('mail_use_ssl', 0))
        timeout = 600  # set timeout to 5mins

        # redirect output to logfile on python2 on python3 debugoutput is caught with overwritten
        # _print_debug function
        if sys.version_info < (3, 0):
            org_smtpstderr = smtplib.stderr
            smtplib.stderr = logger.StderrLogger('worker.smtp')

        log.debug("Start sending email")
        if use_ssl == 2:
            self.asyncSMTP = EmailSSL(self.settings["mail_server"], self.settings["mail_port"],
                                       timeout=timeout)
        else:
            self.asyncSMTP = Email(self.settings["mail_server"], self.settings["mail_port"], timeout=timeout)

        # link to logginglevel
        if logger.is_debug_enabled():
            self.asyncSMTP.set_debuglevel(1)
        if use_ssl == 1:
            self.asyncSMTP.starttls()
        if self.settings["mail_password"]:
            self.asyncSMTP.login(str(self.settings["mail_login"]), str(self.settings["mail_password"]))

        # Convert message to something to send
        fp = StringIO()
        gen = Generator(fp, mangle_from_=False)
        gen.flatten(msg)

        self.asyncSMTP.sendmail(self.settings["mail_from"], self.recipent, fp.getvalue())
        self.asyncSMTP.quit()
        self._handleSuccess()
        log.debug("Email send successfully")

        if sys.version_info < (3, 0):
            smtplib.stderr = org_smtpstderr

    def send_gmail_email(self, message):
        return gmail.send_messsage(self.settings.get('mail_gmail_token', None), message)

    @property
    def progress(self):
        if self.asyncSMTP is not None:
            return self.asyncSMTP.getTransferStatus()
        else:
            return self._progress

    @progress.setter
    def progress(self, x):
        """This gets explicitly set when handle(Success|Error) are called. In this case, remove the SMTP connection"""
        if x == 1:
            self.asyncSMTP = None
            self._progress = x


    @classmethod
    def _get_attachment(cls, bookpath, filename):
        """Get file as MIMEBase message"""
        calibre_path = config.config_calibre_dir
        if config.config_use_google_drive:
            if not (df := gdriveutils.getFileFromEbooksFolder(bookpath, filename)):
                return None
            datafile = os.path.join(calibre_path, bookpath, filename)
            if not os.path.exists(os.path.join(calibre_path, bookpath)):
                os.makedirs(os.path.join(calibre_path, bookpath))
            df.GetContentFile(datafile)
            with open(datafile, 'rb') as file_:
                data = file_.read()
            os.remove(datafile)
        else:
            try:
                with open(os.path.join(calibre_path, bookpath, filename), 'rb') as file_:
                    data = file_.read()
            except IOError as e:
                log.debug_or_exception(e)
                log.error(u'The requested file could not be read. Maybe wrong permissions?')
                return None
        # Set mimetype
        content_type, encoding = mimetypes.guess_type(filename)
        if content_type is None or encoding is not None:
            content_type = 'application/octet-stream'
        main_type, sub_type = content_type.split('/', 1)
        attachment = MIMEBase(main_type, sub_type)
        attachment.set_payload(data)
        encoders.encode_base64(attachment)
        attachment.add_header('Content-Disposition', 'attachment', filename=filename)
        return attachment

    @property
    def name(self):
        return "Email"

    def __str__(self):
        return f"{self.name}, {self.subject}"
